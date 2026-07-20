"""Context compression — the F8 post-rerank, pre-generation token-cost layer (design.md §2-§6).

F6 truncates to the top-5 reranked chunks but the prompt still ships them whole: low-relevance tail
chunks, near-duplicate overlapping fixed-window chunks (F2 emits these by design), and filler
paragraphs the answer never uses — all billed on every gpt-4o-mini generation call. F8 drops that
filler WITHOUT a new model call: dedupe (5-gram Jaccard) → relevance floor (calibrated rerank_score)
→ token-budget greedy fill, sentence-trimming only the one chunk that overflows the budget.

Runs in `baseline._pipeline_events` AFTER the refusal gate (so refusal sees the full reranked
confidence) and BEFORE the generation chain — the CLAUDE.md `rerank → refusal → compress → generate`
order. Best-effort: any failure falls back to the uncompressed chunks and logs `compression_failed`,
so a flaky cross-encoder can never block answering (mirrors F7's rewrite fallback).

Async-mandate placement: sentence scoring reuses F6's `anyio.to_thread.run_sync(model.score, …)`
offload (the same loaded cross-encoder, one set of weights); tiktoken counting, the n-gram/Jaccard
set math, dedupe, and the greedy fill run inline as cheap pure-CPU (same side of the line as F5's RRF
/ F6's sigmoid). No sync twin appears here (the `app/rag/` grep-guard covers this module).
"""

import re
import time

import anyio
import structlog
import tiktoken

from app.core.contracts import RetrievedChunk
from app.rag import observability, trace

logger = structlog.get_logger(__name__)

# Local encoder (NOT an import of baseline._ENC — baseline imports this module, which would cycle).
_ENC = tiktoken.get_encoding("cl100k_base")

# ponytail: regex sentence split, swap for a proper splitter only if regulation text breaks it.
# Split on ., !, ? followed by whitespace; the negative lookbehind keeps section identifiers like
# "15(3)." and single-letter abbreviations from splitting mid-clause on their trailing dot.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def count_tokens(text: str) -> int:
    return len(_ENC.encode(text or ""))


def _score_of(chunk: RetrievedChunk) -> float:
    # None (rerank off/absent) sorts last but is never floored away (AC-3).
    return chunk.rerank_score if chunk.rerank_score is not None else float("-inf")


# --------------------------------------------------------------------------- dedupe (AC-4/AC-5)

def _ngrams(text: str, n: int) -> set[tuple[str, ...]]:
    words = text.lower().split()
    if len(words) < n:  # too short for an n-gram → compare by the full word-set (AC-5)
        return {tuple(words)} if words else set()
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def dedupe(chunks: list[RetrievedChunk], settings) -> tuple[list[RetrievedChunk], int]:
    """Walk in rerank order; drop a chunk whose 5-gram Jaccard vs an already-kept (higher-scored)
    chunk exceeds `COMPRESSION_DEDUPE_JACCARD`. Never drop below `COMPRESSION_MIN_CHUNKS` (top up
    with the highest-scored dropped chunks). Index-based so rerank order + identity stay exact."""
    n = settings.COMPRESSION_DEDUPE_NGRAM
    thresh = settings.COMPRESSION_DEDUPE_JACCARD
    min_chunks = settings.COMPRESSION_MIN_CHUNKS

    kept_idx: list[int] = []
    kept_grams: list[set] = []
    for i, chunk in enumerate(chunks):
        grams = _ngrams(chunk.text, n)
        if any(_jaccard(grams, kg) > thresh for kg in kept_grams):
            continue
        kept_idx.append(i)
        kept_grams.append(grams)

    if len(kept_idx) < min_chunks:
        dropped = sorted(
            (i for i in range(len(chunks)) if i not in set(kept_idx)),
            key=lambda i: _score_of(chunks[i]),
            reverse=True,
        )
        kept_idx = sorted(set(kept_idx) | set(dropped[: min_chunks - len(kept_idx)]))
    kept = [chunks[i] for i in kept_idx]
    return kept, len(chunks) - len(kept)


# ----------------------------------------------------------------------- relevance floor (AC-1/2/3)

def relevance_floor(chunks: list[RetrievedChunk], settings) -> tuple[list[RetrievedChunk], int]:
    """Keep chunks with `rerank_score >= COMPRESSION_SCORE_FLOOR` (a `None` score is always kept —
    no calibrated signal to floor against, AC-3). If fewer than `COMPRESSION_MIN_CHUNKS` survive, top
    up from the dropped set by descending score (AC-2), preserving original rerank order."""
    floor = settings.COMPRESSION_SCORE_FLOOR
    min_chunks = settings.COMPRESSION_MIN_CHUNKS
    if len(chunks) <= min_chunks:
        return chunks, 0

    kept_idx = [
        i for i, c in enumerate(chunks) if c.rerank_score is None or c.rerank_score >= floor
    ]
    if len(kept_idx) < min_chunks:  # top up to the floor by descending score, restore rerank order
        top = sorted(range(len(chunks)), key=lambda i: _score_of(chunks[i]), reverse=True)
        kept_idx = sorted(top[:min_chunks])
    kept = [chunks[i] for i in kept_idx]
    return kept, len(chunks) - len(kept)


# ------------------------------------------------------------- token budget + sentence trim (AC-6/7/8)

def _split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT.split(text.strip()) if s.strip()]


async def _trim_chunk(
    query: str, chunk: RetrievedChunk, budget: int, settings
) -> tuple[RetrievedChunk, int]:
    """Trim `chunk.text` to fit `budget` tokens. If it already fits, return it unchanged. Else score
    every `(query, sentence)` pair in ONE batched off-loop call via the F6 cross-encoder, greedily
    keep the highest-scored sentences whose cumulative tokens fit, and re-emit them in ORIGINAL
    document order (AC-8). Only `text` changes — a `model_copy` preserves every citation field and
    score (AC-10). Never emits empty text: the single top sentence is kept even if it alone exceeds
    the budget."""
    if count_tokens(chunk.text) <= budget:
        return chunk, 0

    sentences = _split_sentences(chunk.text)
    if len(sentences) <= 1:
        return chunk, 0  # nothing to trim within — one sentence, keep it whole

    from app.rag import rerank  # lazy: don't load the cross-encoder deps when compression is off

    model = await rerank.get_rerank_model(settings)
    pairs = [(query, s) for s in sentences]
    logits = await anyio.to_thread.run_sync(model.score, pairs)

    order = sorted(range(len(sentences)), key=lambda i: float(logits[i]), reverse=True)
    keep_idx: set[int] = set()
    used = 0
    for i in order:
        cost = count_tokens(sentences[i])
        if not keep_idx or used + cost <= budget:  # always keep the top sentence (AC-7 non-empty)
            keep_idx.add(i)
            used += cost
        if used >= budget:
            break

    trimmed_text = " ".join(sentences[i] for i in sorted(keep_idx))  # original order
    copy = chunk.model_copy(update={"text": trimmed_text})
    return copy, len(sentences) - len(keep_idx)


async def token_budget_fill(
    query: str, chunks: list[RetrievedChunk], settings
) -> tuple[list[RetrievedChunk], int]:
    """Greedy-fill in rerank order to `COMPRESSION_TOKEN_BUDGET` (AC-6). A chunk that fits is added
    whole; the first overflow chunk is `_trim_chunk`-ed to the remaining budget and chunks after it
    are dropped (AC-7). The first `COMPRESSION_MIN_CHUNKS` chunks are always retained (trimmed if
    needed) so the floor's ≥MIN guarantee survives the budget."""
    budget = settings.COMPRESSION_TOKEN_BUDGET
    min_chunks = min(settings.COMPRESSION_MIN_CHUNKS, len(chunks))

    kept: list[RetrievedChunk] = []
    sentences_dropped = 0
    used = 0
    for chunk in chunks:
        remaining = budget - used
        cost = count_tokens(chunk.text)
        if cost <= remaining:
            kept.append(chunk)
            used += cost
            continue
        # Overflow. Keep this chunk (trimmed) if it's still needed to reach min_chunks, or there's
        # real budget left to fill; otherwise drop it and everything after it.
        must_keep = len(kept) < min_chunks
        if remaining <= 0 and not must_keep:
            break
        # reserve an even share for any still-mandatory chunks so they aren't starved to empty
        mandatory_left = max(min_chunks - len(kept), 1)
        trim_budget = max(remaining // mandatory_left, 1)
        trimmed, n_dropped = await _trim_chunk(query, chunk, trim_budget, settings)
        kept.append(trimmed)
        sentences_dropped += n_dropped
        used += count_tokens(trimmed.text)
        if not must_keep:
            break  # past the mandatory head and we've trimmed the boundary chunk → stop
    return kept, sentences_dropped


# --------------------------------------------------------------------------- orchestrator (AC-12/13)

async def compress_chunks(
    query: str, chunks: list[RetrievedChunk], settings
) -> list[RetrievedChunk]:
    """dedupe → relevance_floor → token_budget_fill, logging `rag.compression` (AC-12). Best-effort:
    any exception logs `compression_failed` and returns the UNCOMPRESSED chunks so answering never
    blocks (AC-13). Adds no OpenAI call — the cost win surfaces as fewer generation input tokens
    through the existing `log_llm_cost` (AC-14)."""
    if not chunks:
        return chunks

    t0 = time.perf_counter()
    tokens_before = sum(count_tokens(c.text) for c in chunks)
    try:
        kept, _ = dedupe(chunks, settings)
        kept, _ = relevance_floor(kept, settings)
        kept, sentences_dropped = await token_budget_fill(query, kept, settings)
    except Exception as exc:  # noqa: BLE001 — compression is best-effort; never propagate (AC-13)
        logger.warning("rag.compression_failed", error=str(exc))
        return chunks

    tokens_after = sum(count_tokens(c.text) for c in kept)
    # What compression actually threw away. `dropped` = whole chunks removed by dedupe or the
    # relevance floor; `trimmed` = chunks that survived but lost sentences to the token budget, as
    # before/after token counts so a shortened passage is distinguishable from an untouched one.
    kept_by_id = {c.chunk_id: c for c in kept}
    trace.record("compressing", {
        "tokens_before": tokens_before,
        "tokens_after": tokens_after,
        "chunks_before": len(chunks),
        "chunks_after": len(kept),
        "sentences_dropped": sentences_dropped,
        "dropped": trace.chunk_rows([c for c in chunks if c.chunk_id not in kept_by_id]),
        "trimmed": [
            {
                "chunk_id": c.chunk_id,
                "title": c.title,
                "tokens_before": count_tokens(c.text),
                "tokens_after": count_tokens(kept_by_id[c.chunk_id].text),
                "text_after": trace.clip(kept_by_id[c.chunk_id].text),
            }
            for c in chunks[:trace.MAX_ITEMS]
            if c.chunk_id in kept_by_id and kept_by_id[c.chunk_id].text != c.text
        ],
    })
    observability.log_compression(
        tokens_before=tokens_before,
        tokens_after=tokens_after,
        chunks_before=len(chunks),
        chunks_after=len(kept),
        sentences_dropped=sentences_dropped,
        compression_ms=int((time.perf_counter() - t0) * 1000),
    )
    return kept
