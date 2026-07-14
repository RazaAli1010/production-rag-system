"""Hybrid retrieval — BM25 (sparse) + dense fused with Reciprocal Rank Fusion (design.md §2-§6).

This is the *body* F5 swaps behind the F3→F5 seam; `retriever.retrieve` (unchanged signature)
dispatches here when hybrid mode is active. Sparse-only hits are hydrated from the F2 metadata
already stored on each Pinecone vector via an async `fetch` (design.md §2), so the seam stays
session-free — no Postgres session is threaded through retrieval.

Async-mandate placement (CLAUDE.md "which side of the line"): the one-time `bm25.pkl` pickle load is
offloaded via `anyio.to_thread.run_sync`; every Pinecone call (dense query, namespace fan-out,
sparse-hit `fetch`) is awaited; `urdu_safe_tokenize`, `BM25Okapi.get_scores` (O(~600 chunks) numpy),
and the RRF dict math run inline as cheap pure-CPU — the same side of the line as the cache-matrix
cosine matmul the mandate permits inline.
"""

import asyncio
import contextvars
import pickle

import anyio
import structlog

from app.core.contracts import RetrievedChunk
from app.indexing.bm25 import urdu_safe_tokenize  # SAME tokenizer that built the corpus (AC-3)
from app.indexing.vectorstore import get_index
from app.rag import errors as errors_mod
from app.rag import retriever as retriever_mod

logger = structlog.get_logger(__name__)

# Loaded once (AC-1), then cached for the process lifetime. Tests reset via `_BM25_CACHE = None`.
_BM25_CACHE: dict | None = None

# Out-of-band degraded signal (AC-14): set inside `hybrid_retrieve`, read+reset by
# `baseline._pipeline_events` onto `AnswerResponse.degraded`. A ContextVar keeps the seam signature
# (`retrieve -> list[RetrievedChunk]`) intact while staying async-task-safe.
_DEGRADED: contextvars.ContextVar[bool] = contextvars.ContextVar("hybrid_degraded", default=False)


class HybridIndexError(RuntimeError):
    """Raised when `bm25.pkl` is missing/unreadable — fail fast (AC-2), never silent dense-only."""


# --------------------------------------------------------------------------- BM25 load (T2)

def _load_pickle_sync(path) -> dict:
    with open(path, "rb") as f:
        return pickle.load(f)


async def load_bm25(settings) -> dict:
    """Load `{bm25, chunk_ids}` from `settings.BM25_PATH` once, off the loop (AC-1). Missing or
    unreadable → `HybridIndexError` naming the path (AC-2)."""
    global _BM25_CACHE
    if _BM25_CACHE is not None:
        return _BM25_CACHE
    path = settings.BM25_PATH
    try:
        cache = await anyio.to_thread.run_sync(_load_pickle_sync, path)
    except FileNotFoundError as exc:
        raise HybridIndexError(
            f"BM25 index not found at {path} — build it with `python -m app.indexing.run`"
        ) from exc
    except Exception as exc:  # pragma: no cover - corrupt pickle is not separately unit-tested
        raise HybridIndexError(f"failed to load BM25 index at {path}: {exc}") from exc
    _BM25_CACHE = cache
    return cache


# ---------------------------------------------------------------------- sparse scoring (T3)

def sparse_scores(query: str, bm25_cache: dict, top_k: int) -> list[tuple[str, float, int]]:
    """Top-`top_k` `(chunk_id, sparse_score, rank)` from BM25 (AC-3). Tokenized with the exact
    `urdu_safe_tokenize` used at build time so query/corpus tokenization never drift and Urdu tokens
    survive. Chunks with no lexical overlap (score ≤ 0) are dropped so they earn no fusion rank.
    Inline pure-CPU (numpy over the small corpus)."""
    bm25 = bm25_cache.get("bm25")
    chunk_ids = bm25_cache.get("chunk_ids") or []
    if bm25 is None or not chunk_ids:
        return []
    tokens = urdu_safe_tokenize(query)
    scores = bm25.get_scores(tokens)  # array aligned 1:1 with chunk_ids
    scored = [(cid, float(s)) for cid, s in zip(chunk_ids, scores, strict=True) if s > 0]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return [(cid, s, rank) for rank, (cid, s) in enumerate(scored[:top_k], start=1)]


# -------------------------------------------------------------------- fetch hydration (T4)

def _none_if_sentinel(value):
    # F2 writes -1 for a null page (Pinecone metadata can't store None) — undo the sentinel.
    return None if value is None or value == -1 else value


def _chunk_from_metadata(chunk_id: str, md: dict) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        doc_id=md["doc_id"],
        title=md["title"],
        text=md.get("text", ""),
        section_heading=md.get("section_heading") or None,
        page_start=_none_if_sentinel(md.get("page_start")),
        page_end=_none_if_sentinel(md.get("page_end")),
        anchor=md.get("anchor") or None,
    )


async def hydrate_sparse_only(
    ids: list[str], namespace: str | None, settings
) -> dict[str, RetrievedChunk]:
    """Hydrate sparse `chunk_id`s to `RetrievedChunk`s from the F2 metadata already on each Pinecone
    vector (AC-4). `namespace=None` fetches across `settings.RETRIEVAL_NAMESPACES` and merges; a
    single namespace fetches only that one (ids in the other namespace return empty, dropped —
    exact filtering of the global BM25 index, design.md §5).

    A `fetch` failure is swallowed per-namespace (logged), returning whatever hydrated: hydration
    also depends on Pinecone, so in degraded mode (dense down) it may fail too — losing the sparse
    metadata must not crash the answer, it just yields fewer/no sparse candidates (→ refusal if
    nothing else), never an unhandled raise past the seam."""
    if not ids:
        return {}
    index = get_index(settings)
    namespaces = [namespace] if namespace is not None else list(settings.RETRIEVAL_NAMESPACES)
    responses = await asyncio.gather(
        *(index.fetch(ids=ids, namespace=ns) for ns in namespaces), return_exceptions=True
    )
    hydrated: dict[str, RetrievedChunk] = {}
    for ns, resp in zip(namespaces, responses, strict=True):
        if isinstance(resp, BaseException):
            logger.warning("hybrid.hydrate_failed", namespace=ns, error=str(resp))
            continue
        for cid, vector in resp.vectors.items():
            if cid in hydrated:
                continue
            hydrated[cid] = _chunk_from_metadata(cid, dict(vector.metadata))
    return hydrated


# ------------------------------------------------------------------------- RRF fusion (T5)

def rrf_fuse(
    dense: list[RetrievedChunk],
    sparse: list[tuple[str, float, int]],
    sparse_chunks: dict[str, RetrievedChunk],
    settings,
) -> list[RetrievedChunk]:
    """Reciprocal Rank Fusion (AC-6/7/8/9). Dedupe by `chunk_id`; `fused_score = Σ 1/(RRF_K + rank)`
    over the lists a chunk appears in; populate `dense_score`/`sparse_score`/`fused_score` (`None`
    where absent); sort by `fused_score` desc; cap at `HYBRID_FUSED_TOP_K`. Pure-CPU, inline."""
    rrf_k = settings.HYBRID_RRF_K
    fused: dict[str, RetrievedChunk] = {}
    fused_score: dict[str, float] = {}

    for rank, chunk in enumerate(dense, start=1):
        copy = chunk.model_copy()  # dense chunk already carries dense_score
        fused[copy.chunk_id] = copy
        fused_score[copy.chunk_id] = fused_score.get(copy.chunk_id, 0.0) + 1.0 / (rrf_k + rank)

    for cid, sscore, srank in sparse:
        if cid in fused:
            fused[cid].sparse_score = sscore
        else:
            hydrated = sparse_chunks.get(cid)
            if hydrated is None:
                continue  # could not hydrate (e.g. wrong namespace) → not a showable candidate
            copy = hydrated.model_copy()  # sparse-only → dense_score stays None
            copy.sparse_score = sscore
            fused[cid] = copy
        fused_score[cid] = fused_score.get(cid, 0.0) + 1.0 / (rrf_k + srank)

    for cid, chunk in fused.items():
        chunk.fused_score = fused_score[cid]
    ordered = sorted(fused.values(), key=lambda c: c.fused_score, reverse=True)
    return ordered[: settings.HYBRID_FUSED_TOP_K]


# ---------------------------------------------------------------- orchestration (T6/T9)

async def hybrid_retrieve(
    query: str, k: int, namespace: str | None, settings
) -> list[RetrievedChunk]:
    """Dense (top-`HYBRID_DENSE_TOP_K`) ∥ sparse (top-`HYBRID_SPARSE_TOP_K`) → hydrate sparse-only →
    RRF fuse → up to `HYBRID_FUSED_TOP_K` candidates (the pool F6 rerank consumes, US-6). The seam
    (`retriever.retrieve`) truncates the returned pool to `k`, so `k` is accepted for a uniform
    signature but not applied here.

    Degraded mode (AC-14): a dense failure — after the F3 retry budget is exhausted via
    `errors.call_with_retry` — falls back to BM25-only and sets the degraded flag, not raising;
    BM25 does not depend on Pinecone."""
    _DEGRADED.set(False)
    bm25_cache = await load_bm25(settings)
    sparse = sparse_scores(query, bm25_cache, settings.HYBRID_SPARSE_TOP_K)

    try:
        dense = await errors_mod.call_with_retry(
            lambda: retriever_mod.dense_retrieve(
                query, settings.HYBRID_DENSE_TOP_K, namespace, settings
            ),
            settings=settings,
        )
    except Exception:
        logger.warning("hybrid.degraded", namespace=namespace, exc_info=True)
        _DEGRADED.set(True)
        dense = []

    dense_ids = {c.chunk_id for c in dense}
    sparse_only_ids = [cid for cid, _, _ in sparse if cid not in dense_ids]
    sparse_chunks = await hydrate_sparse_only(sparse_only_ids, namespace, settings)
    return rrf_fuse(dense, sparse, sparse_chunks, settings)


async def sparse_only(
    query: str, k: int, namespace: str | None, settings
) -> list[RetrievedChunk]:
    """`bm25_only` eval-diagnostic mode (AC-13): BM25 ranking, hydrated, no dense, no fusion.
    `sparse_score` populated; `dense_score`/`fused_score` stay `None`."""
    bm25_cache = await load_bm25(settings)
    sparse = sparse_scores(query, bm25_cache, settings.HYBRID_SPARSE_TOP_K)
    hydrated = await hydrate_sparse_only([cid for cid, _, _ in sparse], namespace, settings)
    out: list[RetrievedChunk] = []
    for cid, sscore, _rank in sparse:
        chunk = hydrated.get(cid)
        if chunk is None:
            continue
        copy = chunk.model_copy()
        copy.sparse_score = sscore
        out.append(copy)
    return out[:k]


def was_degraded() -> bool:
    """Read the degraded flag and reset it (AC-14). Returns False for dense_only/bm25_only paths,
    which never set it."""
    value = _DEGRADED.get()
    _DEGRADED.set(False)
    return value
