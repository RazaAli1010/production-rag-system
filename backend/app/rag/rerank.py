"""Cross-encoder reranking — the F6 precision layer (design.md §2-§6).

F5 fuses dense + BM25 by *rank* (RRF) and never reads the candidate text against the query. F6
reranks F5's fused ≤12 pool with `cross-encoder/ms-marco-MiniLM-L-6-v2`, which jointly encodes each
`(query, chunk_text)` pair, keeps the best `RERANK_TOP_N`, and derives a calibrated confidence
(sigmoid over the top logit) that replaces the crude v1 dense-cosine refusal gate.

This is the *direct* path — the runtime path. We call `HuggingFaceCrossEncoder.score(pairs)`
ourselves (not LangChain's `CrossEncoderReranker.compress_documents`, which reranks but **discards**
the scores) because the raw per-pair scores are what populate `RetrievedChunk.rerank_score` and
drive the confidence gate. `ContextualCompressionRetriever(CrossEncoderReranker(...))` is built as
demonstrable LangChain API surface (`build_compression_retriever`) over the *same* loaded model, but
is never invoked on the request path (design.md §2).

Async-mandate placement (CLAUDE.md "which side of the line"): the one-time model load (blocking
weight load / first-use download) and the per-request `score` forward pass (blocking, CPU-bound) are
the two `anyio.to_thread.run_sync` offloads — PyTorch releases the GIL during the forward pass, so
the worker thread yields real concurrency and token streaming never stalls (the loop-lag probe). The
sigmoid over ≤12 floats and the sort/slice run inline as cheap pure-CPU — the same side of the line
as F5's RRF math.
"""

import asyncio
import contextvars
import math
import time
from typing import Any

import anyio
import structlog
from langchain_community.cross_encoders import HuggingFaceCrossEncoder

from app.core.contracts import RetrievedChunk
from app.rag import observability, trace

logger = structlog.get_logger(__name__)

# Loaded once (AC-1/AC-2), then shared for the process lifetime between the direct scoring path and
# the LangChain CrossEncoderReranker (one set of weights in memory). Tests reset via `_RERANK_MODEL
# = None`. The lock serializes the one-time load so concurrent first requests build it only once.
_RERANK_MODEL: HuggingFaceCrossEncoder | None = None
_MODEL_LOCK = asyncio.Lock()

# Out-of-band timing signal, mirroring hybrid._DEGRADED / rewrite._REWRITE_RESULT: set inside
# `rerank_chunks`, read+reset by `baseline._pipeline_events` to emit the `reranking` stage event.
# rerank runs two levels below the pipeline generator (retriever.retrieve / rewrite.multi_query_
# retrieve), neither of which can yield an SSE event, so the ms comes back out-of-band instead of
# restructuring both callers into generators. A ContextVar keeps the seam signatures intact and
# stays async-task-safe.
_LAST_MS: contextvars.ContextVar[int | None] = contextvars.ContextVar("rerank_ms", default=None)


def last_rerank_ms() -> int | None:
    """Read-and-reset the out-of-band rerank duration, mirroring `hybrid.was_degraded()`. None when
    rerank did not run on this request, so the caller can tell "off" from "ran in 0ms"."""
    ms = _LAST_MS.get()
    _LAST_MS.set(None)
    return ms


# --------------------------------------------------------------------------- model load (T2)

def _build_model(settings) -> HuggingFaceCrossEncoder:
    # device PINNED (settings.RERANK_DEVICE, default "cpu"): HuggingFaceCrossEncoder → CrossEncoder
    # auto-selects CUDA > MPS > CPU, so without this it leaves CPU on a GPU/Apple-silicon dev box
    # (AC-1). Construction imports sentence_transformers and loads the weights — blocking, so this
    # runs inside anyio.to_thread.run_sync (AC-2).
    return HuggingFaceCrossEncoder(
        model_name=settings.RERANK_MODEL, model_kwargs={"device": settings.RERANK_DEVICE}
    )


async def get_rerank_model(settings) -> HuggingFaceCrossEncoder:
    """Return the shared cross-encoder, loading it once off-loop under the lock (AC-1/AC-2)."""
    global _RERANK_MODEL
    if _RERANK_MODEL is not None:
        return _RERANK_MODEL
    async with _MODEL_LOCK:
        if _RERANK_MODEL is None:  # re-check inside the lock — only one loader wins
            _RERANK_MODEL = await anyio.to_thread.run_sync(_build_model, settings)
    return _RERANK_MODEL


async def warm_rerank_model(settings) -> None:
    """Preload hook for F11's startup lifespan (out of scope to wire here) so the first request
    doesn't pay the load. Correctness holds without it — `get_rerank_model` lazy-loads on demand."""
    await get_rerank_model(settings)


# ---------------------------------------------------------------- scoring & calibration (T3/T4)

def _sigmoid(x: float) -> float:
    # Numerically stable both ways so a large negative logit can't overflow math.exp (AC-10).
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    z = math.exp(x)
    return z / (1.0 + z)


def _calibrate(logits: list[float], settings) -> list[float]:
    """Map raw logits → calibrated scores in [0, 1] via sigmoid when `RERANK_APPLY_SIGMOID`
    (AC-10/AC-11). If the model already emits an activated value in [0, 1], the double sigmoid is
    dropped by setting `RERANK_APPLY_SIGMOID=False` — the T6 sanity check pins which case holds for
    this model. Inline pure-CPU (math over ≤12 floats)."""
    if not settings.RERANK_APPLY_SIGMOID:
        return [float(x) for x in logits]
    return [_sigmoid(float(x)) for x in logits]


def _safe_text(chunk: RetrievedChunk) -> str:
    """Guard whitespace-only / empty `text` before scoring so a degenerate pair can't produce a
    garbage score or break the batch (AC-15). Falls back to the section heading or title, which are
    the most query-relevant non-body text we hold, rather than scoring the query against ''."""
    text = (chunk.text or "").strip()
    if text:
        return text
    return (chunk.section_heading or chunk.title or "").strip()


# --------------------------------------------------------------------------- rerank (T3/T5)

def max_rerank_score(chunks: list[RetrievedChunk]) -> float:
    """The calibrated confidence for the refusal gate (AC-10): the top reranked chunk's score, or
    0.0 when nothing was reranked (empty pool / rerank-off path)."""
    scores = [c.rerank_score for c in chunks if c.rerank_score is not None]
    return max(scores) if scores else 0.0


async def rerank_chunks(
    query: str, pool: list[RetrievedChunk], settings
) -> list[RetrievedChunk]:
    """Direct-path rerank (the runtime path). Take F5's fused pool (≤`HYBRID_FUSED_TOP_K`), score
    every `(query, text)` pair in **one** batched call off the loop, calibrate, reorder the whole
    chunk objects by score, and return the top `RERANK_TOP_N` (AC-6/7/9).

    Empty pool → short-circuit `[]` with no model call (AC-14). Scores bind to whole objects so
    `rerank_score`/metadata/text stay together through the sort + slice (AC-9). `rerank_ms` logged
    (AC-8/AC-20)."""
    if not pool:
        observability.log_rerank(rerank_ms=0, max_score=0.0, n_candidates=0)
        _LAST_MS.set(0)  # ran, just had nothing to score — distinct from None (= rerank off)
        return []

    t0 = time.perf_counter()
    model = await get_rerank_model(settings)
    pairs = [(query, _safe_text(c)) for c in pool]  # guarded (AC-15); one batch (AC-7)
    # Explicit offload (AC-4): the blocking forward pass runs on a worker thread so the event loop
    # keeps streaming tokens / serving concurrent asks. NOT LangChain's opaque executor fallback.
    logits = await anyio.to_thread.run_sync(model.score, pairs)
    scores = _calibrate(list(logits), settings)

    reranked: list[RetrievedChunk] = []
    for chunk, score in zip(pool, scores, strict=True):
        copy = chunk.model_copy()  # carry dense/sparse/fused scores through untouched
        copy.rerank_score = score
        reranked.append(copy)
    reranked.sort(key=lambda c: c.rerank_score, reverse=True)  # whole objects (AC-9)
    top = reranked[: settings.RERANK_TOP_N]

    rerank_ms = int((time.perf_counter() - t0) * 1000)
    # before/after, with each surviving chunk's movement — the one stage whose effect is invisible
    # from timings alone. `moved` is (old rank - new rank): positive = cross-encoder promoted it.
    before_rank = {c.chunk_id: i for i, c in enumerate(pool)}
    trace.record("reranking", {
        "query": trace.clip(query),
        "n_candidates": len(pool),
        "kept": len(top),
        "before": trace.chunk_rows(pool, "fused_score"),
        "after": [
            dict(trace.chunk_row(c, "rerank_score"), moved=before_rank[c.chunk_id] - i)
            for i, c in enumerate(top[:trace.MAX_ITEMS])
        ],
    })
    observability.log_rerank(
        rerank_ms=rerank_ms, max_score=max_rerank_score(top), n_candidates=len(pool)
    )
    _LAST_MS.set(rerank_ms)
    return top


# ------------------------------------------------------------- LangChain API surface (T9, AC-3)

def _chunk_to_document(chunk: RetrievedChunk):
    from langchain_core.documents import Document

    return Document(
        id=chunk.chunk_id,
        page_content=chunk.text,
        metadata={
            "doc_id": chunk.doc_id,
            "title": chunk.title,
            "section_heading": chunk.section_heading,
            "page_start": chunk.page_start,
            "page_end": chunk.page_end,
            "anchor": chunk.anchor,
        },
    )


def _make_base_retriever(settings, namespace: str | None = None, k: int = 5):
    """A thin BaseRetriever adapter over F5's hybrid_retrieve so the LangChain compression retriever
    (AC-3) has a `base_retriever`. Test-only wiring — never on the request path."""
    from langchain_core.retrievers import BaseRetriever

    class HybridBaseRetriever(BaseRetriever):
        # BaseRetriever is a pydantic model; declare the fields we carry.
        rag_settings: Any = None
        namespace: str | None = None
        k: int = 5

        async def _aget_relevant_documents(self, query: str, *, run_manager):
            from app.rag import hybrid

            chunks = await hybrid.hybrid_retrieve(query, self.k, self.namespace, self.rag_settings)
            return [_chunk_to_document(c) for c in chunks]

        def _get_relevant_documents(self, query: str, *, run_manager):
            # Async-only per the CLAUDE.md mandate; the compression retriever is exercised via its
            # async surface in the test.
            raise NotImplementedError("HybridBaseRetriever is async-only")

    return HybridBaseRetriever(rag_settings=settings, namespace=namespace, k=k)


async def build_compression_retriever(settings, base_retriever=None):
    """API surface ONLY (AC-3), off the runtime path. Builds
    `ContextualCompressionRetriever(base_compressor=CrossEncoderReranker(model=<shared model>,
    top_n=RERANK_TOP_N), base_retriever=<F5 hybrid retriever>)` over the **same** cross-encoder
    instance the direct path uses (zero extra memory). Imported lazily from `langchain_classic` (the
    package that ships these classic retrievers in langchain 1.x) so the runtime path never pulls
    them in. Covered by a test; NEVER invoked during generation — it re-retrieves (design.md §2)."""
    from langchain_classic.retrievers import ContextualCompressionRetriever
    from langchain_classic.retrievers.document_compressors import CrossEncoderReranker

    model = await get_rerank_model(settings)  # the SAME shared instance (AC-3)
    compressor = CrossEncoderReranker(model=model, top_n=settings.RERANK_TOP_N)
    base = base_retriever if base_retriever is not None else _make_base_retriever(settings)
    return ContextualCompressionRetriever(base_compressor=compressor, base_retriever=base)
