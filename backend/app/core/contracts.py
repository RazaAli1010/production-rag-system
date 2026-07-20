from typing import Literal

from pydantic import BaseModel


class Chunk(BaseModel):
    chunk_id: str
    doc_id: str
    seq: int
    text: str
    section_heading: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    anchor: str | None = None
    token_count: int


class RetrievedChunk(BaseModel):
    """Transient, per-query — never persisted (mirrors `RetrievedChunk` in corpus.py's docstring).

    F3 populates `dense_score` only; F5/F6 populate `sparse_score`/`fused_score`/`rerank_score`
    on the exact same model without a schema change (design.md §10).
    """

    chunk_id: str
    doc_id: str
    title: str
    text: str
    section_heading: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    anchor: str | None = None
    dense_score: float | None = None
    sparse_score: float | None = None
    fused_score: float | None = None
    rerank_score: float | None = None


class RewriteResult(BaseModel):
    """Transient, per-query — never persisted (mirrors `RetrievedChunk`). The output of F7's single
    `gpt-4o-mini` rewrite call: `normalized` (cleaned/translated/condensed standalone query),
    `variants` (paraphrases for multi-query fan-out), `language` (passed explicitly to generation).
    `failed=True` marks the raw-query fallback taken when the rewrite call fails (design.md §4)."""

    normalized: str
    variants: list[str] = []
    language: Literal["en", "ur-mix"] | None = None
    failed: bool = False

    def fanout_queries(self) -> list[str]:
        """`dedupe([normalized, *variants])` preserving order — the retrieval fan-out set (AC-5).
        Drops blank entries so a degenerate variant never becomes an empty retrieval query."""
        seen: set[str] = set()
        out: list[str] = []
        for q in [self.normalized, *self.variants]:
            q = (q or "").strip()
            if q and q not in seen:
                seen.add(q)
                out.append(q)
        return out


class Citation(BaseModel):
    """Resolved `[n]` marker — or a pre-LLM "you might check" suggestion (AC-7)."""

    chunk_id: str
    doc_id: str
    title: str
    section_heading: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    # `None` only for pre-LLM refusal suggestions (refusal.suggestion_citations is a synchronous,
    # DB-free function per design.md §4 — it builds straight from the already-in-memory
    # RetrievedChunk, which carries no `url` since F2 never wrote one into Pinecone metadata).
    # citations.parse_citations always populates a real url from `documents.url` (NOT NULL).
    url: str | None = None
    quote: str  # always derived via context.extract_quote(), never LLM-authored (AC-16)


class ChatMessage(BaseModel):
    role: Literal["user", "assistant", "system"]
    content: str


class MemoryContext(BaseModel):
    """Pre-assembled by F17; F3 only accepts and renders it (AC-24), never builds it."""

    summary: str | None = None
    pairs: list[ChatMessage] = []
    summarized: bool = False
    # F17: additive with defaults (same pattern as F9's AnswerResponse.tokens_in/out). Pre-F17
    # producers (F3/F4/F9) construct MemoryContext without these; the F17 window assembler sets them.
    window_pairs: int = 0  # window size actually used this turn (AC-19/20 assert on it)
    effective_tokens: int = 0  # tokens of summary + pairs (over-budget test asserts < budget)


class StageEvent(BaseModel):
    """The stage vocabulary F3 emits (`searching`/`generating`/`citing`); F17/F14 only add
    stages, never replace this shape."""

    stage: str
    status: Literal["started", "done", "skipped"]
    ms: int | None = None
    # The stage's intermediate output (see `rag/trace.py`) — what it actually retrieved, reordered
    # or discarded, so the pipeline can be inspected rather than inferred from timings. Present on
    # `done` frames only, and only while `ENABLE_TRACE` is on. Additive with a default, same
    # discipline as `AnswerResponse.degraded`: every existing producer and consumer is unchanged.
    detail: dict | None = None


class PipelineFlags(BaseModel):
    """Inert, forward-declared toggles (Phase A) — all `False` until F5–F9/F17 wire them up.

    Lives here (not only re-exported from `app.rag.schemas`) so `AnswerResponse` can reference it
    without a circular import between `app.core.contracts` and `app.rag.schemas`.
    """

    hybrid: bool = False
    rerank: bool = False
    query_rewrite: bool = False
    compression: bool = False
    cache: bool = False
    memory: bool = False


class AnswerResponse(BaseModel):
    answer: str
    citations: list[Citation] = []
    refused: bool = False
    refusal_reason: str | None = None
    pipeline_flags: PipelineFlags
    session_id: str | None = None
    memory_summarized: bool = False
    cache_hit: bool = False
    # F9: the tokens this answer cost to produce. The canonical Shared Context contract always
    # specified these; F3 computed both as locals in `_pipeline_events` and dropped them on the
    # floor. F9 is the first feature that needs them — a cache hit must report the spend it
    # avoided (`estimate_cost(model, tokens_in, tokens_out)`) — so F9 restores them. Additive with
    # defaults, exactly like `degraded` below: every prior F3/F4 path and test is unchanged, and
    # AnswerResponse is not a table so there is no migration. Riding on `meta` is also what lets
    # the F4 latency suite compute `cache_cost_saved_mean` without extra plumbing.
    # `request_id`/`latency_ms` stay absent — F9 has no consumer and F13 owns request identity.
    tokens_in: int = 0
    tokens_out: int = 0
    # F5: True when hybrid retrieval fell back to BM25-only because the dense (Pinecone) query
    # failed (AC-14/AC-17). Additive, non-persisted contract field — default False keeps every
    # prior F3/F4 path and test unchanged; no Alembic migration (AnswerResponse is not a table).
    degraded: bool = False
    # F11: request correlation id + server wall-clock, the two identity fields the canonical
    # contract always reserved ("F13 owns request identity" held only because no consumer existed).
    # The ask route stamps both from the request contextvar + a route timer — NOT threaded through
    # baseline.py. Additive with defaults, same discipline as `degraded`/`tokens_in`; no migration.
    request_id: str | None = None
    latency_ms: int | None = None
