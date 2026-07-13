"""`request_logs`, `cache_entries` — F13 / F9 state (design.md §3.5).

Embedding stays BYTEA (not pgvector): Pinecone is the vector store; `cache_entries.embedding` is
only compared in-process by F9's cosine matmul, so no DB-side vector ops are needed.
"""

import uuid

from sqlalchemy import Enum, ForeignKey, LargeBinary
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.enums import RequestChannel
from app.db.types import CreatedAt, JSONBDict, TZDateTime, UUIDpk


class RequestLog(Base):  # AC-3.8 — every field F13 logs
    __tablename__ = "request_logs"

    request_id: Mapped[str] = mapped_column(primary_key=True)
    ts: Mapped[CreatedAt]
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sessions.id", ondelete="SET NULL")
    )
    channel: Mapped[RequestChannel] = mapped_column(Enum(RequestChannel, name="request_channel"))
    query_hash: Mapped[str]
    pipeline_flags: Mapped[JSONBDict]
    cache_hit: Mapped[bool]
    refused: Mapped[bool]
    degraded: Mapped[bool]
    memory_summarized: Mapped[bool]
    embed_ms: Mapped[int | None]
    retrieve_ms: Mapped[int | None]
    rerank_ms: Mapped[int | None]
    rewrite_ms: Mapped[int | None]
    memory_ms: Mapped[int | None]
    summarize_ms: Mapped[int | None]
    llm_ms: Mapped[int | None]
    total_ms: Mapped[int | None]
    tokens_in: Mapped[int]
    tokens_out: Mapped[int]
    est_cost_usd: Mapped[float]
    model: Mapped[str]
    http_status: Mapped[int]
    error_type: Mapped[str | None]


class CacheEntry(Base):  # AC-3.9
    __tablename__ = "cache_entries"

    id: Mapped[UUIDpk]
    query_text: Mapped[str]
    embedding: Mapped[bytes] = mapped_column(LargeBinary)  # float32[1536] ~= 6 KB
    answer: Mapped[JSONBDict]  # serialized AnswerResponse
    index_manifest_id: Mapped[str]  # invalidate on reindex
    hits: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[CreatedAt]
    last_hit_at: Mapped[TZDateTime | None]
