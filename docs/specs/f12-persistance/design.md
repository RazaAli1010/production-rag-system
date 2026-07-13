# F12 — Persistence Layer · design.md

**Module:** `backend/app/db/` · **Phase A · build first · blocks everything.**

---

## 1. Module layout

```
backend/app/db/
├── __init__.py            # re-exports get_session, Base, models
├── base.py                # DeclarativeBase + MetaData naming convention + shared mixins
├── engine.py              # build_engine(), get_engine(), get_sessionmaker()
├── session.py             # get_session() FastAPI dependency (async generator)
├── enums.py               # DocumentStatus, UserRole, MessageRole, RequestChannel
├── types.py               # reusable typed columns (UUIDpk, TZDateTime, JSONBDict)
├── models/
│   ├── __init__.py        # imports every model so Alembic autogenerate sees them
│   ├── user.py            # User, ApiKey
│   ├── auth.py            # RefreshToken, LoginAttempt
│   ├── corpus.py          # Document, Chunk
│   ├── chat.py            # Session, Message
│   ├── ops.py             # RequestLog, CacheEntry
│   └── evals.py           # EvalRun, EvalResult
├── seed.py                # async seed_admin() from env
└── migrations/            # Alembic
    ├── env.py             # async online migrations
    ├── script.py.mako
    └── versions/
        └── 0001_initial.py
```

`backend/app/core/settings.py` gains the DB keys (§6). `docker/docker-compose.yml`,
`Makefile`, and `.github/workflows/ci.yml` gain the dev/CI wiring (§8).

### Why this split
Models are grouped by bounded context (`corpus`, `chat`, `ops`, `evals`, `user`, `auth`) rather
than one giant `models.py`, so the feature that owns a table (F1 corpus, F17 chat, F9/F13 ops,
F4 evals, F10 auth) has an obvious file to extend — without F12 taking on that feature's logic.
`models/__init__.py` imports all of them so Alembic's `target_metadata` sees the full schema.

---

## 2. Base, metadata & shared types

```python
# base.py
from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
}

class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
```

Deterministic constraint names make Alembic autogenerate diffs stable and reviewable (AC-4.3).

```python
# types.py  (SQLAlchemy 2.0 typed style)
import uuid, datetime as dt
from typing import Annotated
from sqlalchemy import String, DateTime, func
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import mapped_column

UUIDpk    = Annotated[uuid.UUID, mapped_column(UUID(as_uuid=True),
                       primary_key=True, default=uuid.uuid4)]
TZDateTime = Annotated[dt.datetime, mapped_column(DateTime(timezone=True))]
CreatedAt  = Annotated[dt.datetime, mapped_column(DateTime(timezone=True),
                       server_default=func.now())]
JSONBDict  = Annotated[dict, mapped_column(JSONB)]
```

All timestamps are `timezone=True` (`timestamptz`); the app is UTC end-to-end.

---

## 3. Models (mirroring the Shared-Context contracts)

Only field/shape decisions that matter for the contract mapping are shown; obvious columns are
elided. Every model subclasses `Base`.

### 3.1 `users`, `api_keys` (`user.py`) — owned schema, F10 logic
```python
class User(Base):
    __tablename__ = "users"
    id: Mapped[UUIDpk]
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    hashed_password: Mapped[str]
    role: Mapped[UserRole] = mapped_column(Enum(UserRole, name="user_role"),
                                           default=UserRole.student)
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[CreatedAt]

class ApiKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[UUIDpk]
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    key_hash: Mapped[str]
    label: Mapped[str | None]
    created_at: Mapped[CreatedAt]
    revoked_at: Mapped[TZDateTime | None]
```

### 3.2 `refresh_tokens`, `login_attempts` (`auth.py`) — the blacklist + lockout feed
```python
class RefreshToken(Base):                     # AC-3.6: this table IS the blacklist
    __tablename__ = "refresh_tokens"
    id: Mapped[UUIDpk]
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    jti: Mapped[str] = mapped_column(unique=True, index=True)
    issued_at: Mapped[CreatedAt]
    expires_at: Mapped[TZDateTime]
    revoked_at: Mapped[TZDateTime | None]      # NULL + not expired == valid
    replaced_by_jti: Mapped[str | None]        # rotation chain
    user_agent: Mapped[str | None]
    ip: Mapped[str | None]

class LoginAttempt(Base):                      # AC-3.7: windowed lockout counter
    __tablename__ = "login_attempts"
    id: Mapped[UUIDpk]
    email_or_ip: Mapped[str] = mapped_column(index=True)
    attempted_at: Mapped[CreatedAt] = mapped_column(index=True)
    success: Mapped[bool]
```
Validity rule (used by F10, not implemented here): `revoked_at IS NULL AND expires_at > now()`.
Lockout query (F10): `count(*) WHERE email_or_ip=:k AND success=false AND attempted_at > now()-15min`.
A composite index on `(email_or_ip, attempted_at)` backs that window; old rows pruned on schedule (F13/cron).

### 3.3 `documents`, `chunks` (`corpus.py`) — mirror `DocumentMeta` / `Chunk`
```python
class Document(Base):                          # mirrors DocumentMeta + status
    __tablename__ = "documents"
    doc_id: Mapped[str] = mapped_column(primary_key=True)   # slug+year, e.g. hec-plagiarism-policy-2021
    title: Mapped[str]
    source_org: Mapped[str]                    # "PU" | "HEC" (CHECK)
    url: Mapped[str]
    file_type: Mapped[str]                     # pdf|html|docx|pptx|xlsx (CHECK)
    downloaded_at: Mapped[TZDateTime]
    version_label: Mapped[str]
    is_scanned: Mapped[bool]
    page_count: Mapped[int | None]
    sha256: Mapped[str] = mapped_column(index=True)
    status: Mapped[DocumentStatus] = mapped_column(
        Enum(DocumentStatus, name="document_status"), default=DocumentStatus.registered)

class Chunk(Base):                             # mirrors Chunk contract
    __tablename__ = "chunks"
    chunk_id: Mapped[str] = mapped_column(primary_key=True)  # {doc_id}:{chunk_seq}
    doc_id: Mapped[str] = mapped_column(ForeignKey("documents.doc_id", ondelete="CASCADE"))
    seq: Mapped[int]
    text: Mapped[str]
    section_heading: Mapped[str | None]
    page_start: Mapped[int | None]
    page_end: Mapped[int | None]
    anchor: Mapped[str | None]                 # HTML anchor / slide no. / sheet name
    token_count: Mapped[int]
    __table_args__ = (Index("ix_chunks_doc_id_seq", "doc_id", "seq"),)  # AC-3.3
```
`RetrievedChunk` (dense/sparse/fused/rerank scores) is a **transient** runtime model, not
persisted — scores are recomputed per query, so no columns for them. Recorded as a deliberate
decision.

### 3.4 `sessions`, `messages` (`chat.py`) — F17 state
```python
class Session(Base):
    __tablename__ = "sessions"
    id: Mapped[UUIDpk]
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"))   # anonymous allowed (AC-3.5)
    title: Mapped[str | None]                            # auto from first question
    total_tokens: Mapped[int] = mapped_column(default=0) # running tiktoken sum of ALL messages
    summary: Mapped[str | None]
    summary_token_count: Mapped[int | None]
    summarized_upto_message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL", use_alter=True))
    created_at: Mapped[CreatedAt]
    last_active_at: Mapped[TZDateTime] = mapped_column(server_default=func.now())
    is_archived: Mapped[bool] = mapped_column(default=False)
    __table_args__ = (Index("ix_sessions_user_id_last_active_at", "user_id", "last_active_at"),)

class Message(Base):                            # mirrors ChatMessage
    __tablename__ = "messages"
    id: Mapped[UUIDpk]
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"))
    role: Mapped[MessageRole] = mapped_column(Enum(MessageRole, name="message_role"))
    content: Mapped[str]
    token_count: Mapped[int]                    # tiktoken cl100k_base
    citations: Mapped[JSONBDict | None]         # list[Citation] serialized; assistant turns only
    refused: Mapped[bool] = mapped_column(default=False)
    request_id: Mapped[str | None]
    created_at: Mapped[CreatedAt]
    __table_args__ = (Index("ix_messages_session_id_created_at", "session_id", "created_at"),)
```
Note the `sessions.summarized_upto_message_id → messages.id` FK is a circular reference with
`sessions ← messages.session_id`; resolved with `use_alter=True` so Alembic emits the FK as a
post-create `ALTER TABLE` (see §7).

### 3.5 `request_logs`, `cache_entries` (`ops.py`) — F13 / F9 state
```python
class RequestLog(Base):                         # AC-3.8 — every field F13 logs
    __tablename__ = "request_logs"
    request_id: Mapped[str] = mapped_column(primary_key=True)
    ts: Mapped[CreatedAt]
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    session_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sessions.id", ondelete="SET NULL"))
    channel: Mapped[RequestChannel]             # web|telegram|api
    query_hash: Mapped[str]
    pipeline_flags: Mapped[JSONBDict]
    cache_hit: Mapped[bool]
    refused: Mapped[bool]
    degraded: Mapped[bool]
    memory_summarized: Mapped[bool]
    embed_ms:   Mapped[int | None]; retrieve_ms: Mapped[int | None]
    rerank_ms:  Mapped[int | None]; rewrite_ms:  Mapped[int | None]
    memory_ms:  Mapped[int | None]; summarize_ms: Mapped[int | None]
    llm_ms:     Mapped[int | None]; total_ms:    Mapped[int | None]
    tokens_in: Mapped[int]; tokens_out: Mapped[int]
    est_cost_usd: Mapped[float]
    model: Mapped[str]
    http_status: Mapped[int]
    error_type: Mapped[str | None]

class CacheEntry(Base):                          # AC-3.9
    __tablename__ = "cache_entries"
    id: Mapped[UUIDpk]
    query_text: Mapped[str]
    embedding: Mapped[bytes] = mapped_column(LargeBinary)   # float32[1536] ≈ 6 KB
    answer: Mapped[JSONBDict]                                # serialized AnswerResponse
    index_manifest_id: Mapped[str]                           # invalidate on reindex
    hits: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[CreatedAt]
    last_hit_at: Mapped[TZDateTime | None]
```

### 3.6 `eval_runs`, `eval_results` (`evals.py`) — F4 state
```python
class EvalRun(Base):
    __tablename__ = "eval_runs"
    id: Mapped[UUIDpk]
    label: Mapped[str]
    git_sha: Mapped[str]
    index_manifest: Mapped[JSONBDict]
    pipeline_flags: Mapped[JSONBDict]
    started_at: Mapped[CreatedAt]

class EvalResult(Base):
    __tablename__ = "eval_results"
    id: Mapped[UUIDpk]
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("eval_runs.id", ondelete="CASCADE"))
    metric: Mapped[str]                          # hit@5, mrr, faithfulness, answer_relevancy, latency, cost
    value: Mapped[float]
    slice_tag: Mapped[str | None]                # e.g. code_switched
```

### JSONB vs normalized columns (edge-case decision, AC required)
`pipeline_flags`, `citations`, `answer`, `index_manifest` are stored as **JSONB**:
- They are write-once, read-as-whole blobs consumed by the owning feature's Pydantic model —
  they are never filtered/joined on individual keys in hot paths.
- Their shape evolves with features (flags added in F5–F9); JSONB avoids an Alembic migration
  per new flag.
- Per-stage timings, by contrast, ARE normalized columns because they are aggregated/queried in
  dashboards (F13), where indexed scalar columns beat JSONB extraction.

Embedding stays BYTEA (not pgvector): Pinecone is the vector store; `cache_entries.embedding`
is only compared in-process by F9's cosine matmul, so no DB-side vector ops are needed.

---

## 4. Engine & session (the seam everything imports)

```python
# engine.py
from functools import lru_cache
from sqlalchemy.ext.asyncio import (AsyncEngine, async_sessionmaker,
                                    create_async_engine, AsyncSession)
from app.core.settings import settings

def _connect_args() -> dict:
    # AC-1.4 — pgbouncer/session-pooler needs prepared statements off
    if settings.DB_STATEMENT_CACHE_SIZE == 0:
        return {"statement_cache_size": 0, "prepared_statement_cache_size": 0}
    return {}

@lru_cache
def get_engine() -> AsyncEngine:
    return create_async_engine(
        str(settings.DATABASE_URL),           # postgresql+asyncpg://...
        pool_size=settings.DB_POOL_SIZE,      # ≤ 5 (AC-1.2)
        max_overflow=settings.DB_MAX_OVERFLOW,
        pool_timeout=settings.DB_POOL_TIMEOUT,
        pool_recycle=settings.DB_POOL_RECYCLE,
        pool_pre_ping=True,                   # drop stale free-tier conns
        echo=settings.DB_ECHO,
        connect_args=_connect_args(),
    )

@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False, autoflush=False)
```

```python
# session.py
from typing import AsyncIterator
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.engine import get_sessionmaker

async def get_session() -> AsyncIterator[AsyncSession]:      # FastAPI dependency (AC-1.3)
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        # `async with` closes/returns the connection to the pool
```

Routers use `session: AsyncSession = Depends(get_session)`. Write-behind logging (F13) uses the
same sessionmaker inside `asyncio.create_task` with its own short-lived session, never the
request-scoped one (a background task must not outlive the request's session).

### Async-rule compliance (AC-1.5, AC-1.6)
Every path here is `async`/`await` over asyncpg. F12 performs **no CPU-bound work**, so it
introduces **no** `anyio.to_thread`/`run_in_executor` offload — stated explicitly per the global
"which side of the line" rule. (bcrypt hashing lives in F10, not here.) The sync SQLAlchemy API
is never imported in `backend/app/`; the ruff/grep CI guard covers `db/`.

---

## 5. Data-flow diagram

```
                        ┌───────────────────────── app startup ─────────────────────────┐
                        │  get_engine()  ── asyncpg pool (size ≤5, pre_ping, recycle)     │
                        └───────────────────────────────────────────────────────────────┘
                                                   │
 HTTP request ─► FastAPI router ─► Depends(get_session) ─► AsyncSession (pool checkout)
                                                   │
        ┌──────────────────────────────────────────┼───────────────────────────────────┐
        ▼                     ▼                      ▼                     ▼              ▼
  F10 auth  (users,     F1 ingest (documents,   F17 chat (sessions,   F9 cache      F4 evals
  api_keys, refresh_    chunks)                 messages)             (cache_        (eval_runs,
  tokens, login_                                                     entries)        eval_results)
  attempts)
                                                   │
                              commit on success / rollback on error ─► pool return

 Background (write-behind, asyncio.create_task):  F13 ─► RequestLog  (own short-lived session)

 Migrations (offline path):  alembic upgrade head ─► migrations/env.py (async) ─► 0001_initial
```

`StageEvent` is transient SSE payload (not persisted); its timings land in `request_logs` scalar
columns. `MemoryContext` is computed by F17 from `sessions`+`messages`, never stored directly.

---

## 6. New Settings keys (central Pydantic `Settings`)

| Key | Type | Default | Purpose |
|---|---|---|---|
| `DATABASE_URL` | `PostgresDsn` | — (required) | asyncpg URL; already declared globally, consumed here |
| `DB_POOL_SIZE` | `int` | `5` | free-tier-safe pool cap (AC-1.2) |
| `DB_MAX_OVERFLOW` | `int` | `2` | burst headroom; documented overflow rule |
| `DB_POOL_TIMEOUT` | `int` (s) | `30` | wait before pool-exhaustion error |
| `DB_POOL_RECYCLE` | `int` (s) | `1800` | recycle before free-tier idle cutoff |
| `DB_STATEMENT_CACHE_SIZE` | `int` | `0` | `0` ⇒ pgbouncer-safe (AC-1.4); `>0` for direct conns |
| `DB_ECHO` | `bool` | `false` | SQL echo for local debug |
| `ADMIN_EMAIL` | `EmailStr` | — | seed admin (AC-5.2) |
| `ADMIN_PASSWORD` | `SecretStr` | — | seed admin (AC-5.2) |

No other feature's flags are touched. All keys live in the one `Settings` class per global
convention.

---

## 7. Alembic migrations

- `alembic init` (async template); `migrations/env.py` imports `app.db.models` so
  `target_metadata = Base.metadata` sees all tables, and runs online migrations through the async
  engine (`connectable.begin()` inside `asyncio.run`).
- **`0001_initial.py`** creates, in order: PG enums (`user_role`, `document_status`,
  `message_role`, request-channel), then tables with FKs, then the circular
  `sessions.summarized_upto_message_id → messages.id` FK via a post-create
  `op.create_foreign_key(...)` (matching the `use_alter=True` model hint), then all declared
  indexes. Downgrade drops in reverse and drops the enums.
- Naming convention (§2) guarantees autogenerate emits stable constraint names, so future diffs
  are clean (AC-4.3). Every later schema change is its own migration (AC-4.4).

---

## 8. Local dev & CI

- `docker/docker-compose.yml`: `postgres:16` + `redis:7` (Upstash-compatible), volumes,
  healthchecks. `Makefile`: `db-up` (`docker compose up -d postgres redis`), `migrate`
  (`alembic upgrade head`), `db-down`, `seed` (`python -m app.db.seed`).
- `seed.py`: `async def seed_admin()` — `SELECT` by email; if absent, insert `User` with role
  `admin` and hashed password (delegates hashing to F10's `passlib` helper once available; until
  then a local bcrypt call run via `anyio.to_thread`). Idempotent (AC-5.2).
- CI (`.github/workflows/ci.yml`): Postgres service container → `alembic upgrade head` →
  `pytest backend/tests/db` (CRUD smoke per model) → ruff/grep async-guard over `backend/app/`.
  Job fails on any migration or test failure (AC-6.1, AC-6.2).

---

## 9. Error handling

| Condition | Handling |
|---|---|
| Pool exhausted (free-tier cap) | `pool_timeout` raises `TimeoutError`; surfaced by F11 as 503 `degraded=true`; `pool_pre_ping` + `pool_recycle` reduce stale-conn churn. |
| pgbouncer + prepared statements | `DB_STATEMENT_CACHE_SIZE=0` → asyncpg caches disabled (AC-1.4); documented in README. |
| Unique violation (e.g. duplicate email) | `IntegrityError` bubbles to the owning feature (F10) which maps to 409; `get_session` rolls back. |
| Unhandled error mid-request | `get_session` `except` block rolls back and re-raises; no partial commit. |
| Migration drift | CI `alembic upgrade head` on a fresh container catches missing migrations. |
| Circular session↔message FK | resolved via `use_alter=True` + post-create FK in `0001` (§7). |

---

## 10. How this honors Shared Context & the F3 LCEL seam

- **Contracts:** `documents`/`chunks`/`messages` columns are a 1:1 mirror of `DocumentMeta`,
  `Chunk`, and `ChatMessage`; `Citation`/`AnswerResponse` are persisted as JSONB blobs consumed
  by their owning Pydantic models — round-trip with no bespoke mapping (US-5).
- **LCEL retriever seam (F3):** F12 itself imports no LangChain. It provides (a) the `chunks`
  rows F2 writes and F3's retriever reads document/section/page metadata from for `Citation`
  assembly, and (b) the async `get_session` dependency that F3's chains use for write-behind
  request logging via `asyncio.create_task` — so persistence never blocks the `astream_events`
  pipeline or the SSE event loop.
- **Async everywhere:** async engine + async sessions only; no sync API in `backend/app/`; no
  CPU offload needed here (§4).
- **Vector store:** pgvector deliberately unused; Pinecone owns vectors; `cache_entries.embedding`
  is BYTEA compared in-process by F9 (recorded decision).
