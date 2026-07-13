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


class StageEvent(BaseModel):
    """The stage vocabulary F3 emits (`searching`/`generating`/`citing`); F17/F14 only add
    stages, never replace this shape."""

    stage: str
    status: Literal["started", "done", "skipped"]
    ms: int | None = None


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
