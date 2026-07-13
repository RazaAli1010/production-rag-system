# F12 — Persistence Layer · requirements.md

**Module:** `backend/app/db/`
**Phase:** A (foundation) · **Build order:** FIRST (numbered 12 for historical reasons)
**Depends on:** nothing · **Blocks:** everything (F1–F17)

> Scope note: F12 owns the schema, async engine/session, and migrations for the tables listed
> in Shared Context (`users`, `api_keys`, `refresh_tokens`, `login_attempts`, `documents`,
> `chunks`, `sessions`, `messages`, `request_logs`, `cache_entries`, `eval_runs`,
> `eval_results`). It does **not** own the business logic that reads/writes those tables —
> auth flows live in F10, memory logic in F17, cache logic in F9, eval writes in F4, request
> logging in F13. F12 provides the tables, the typed models, and the `get_session` seam those
> features build on. pgvector is deliberately **not** used (Pinecone is the vector store).

---

## User stories

- **US-1 (async session seam).** As a backend developer, I want a single async
  `get_session` FastAPI dependency backed by a pooled asyncpg engine, so every router and
  chain shares one connection-pool configuration that stays inside free-tier connection caps.
- **US-2 (managed-Postgres portability).** As a platform engineer, I want the app to boot
  unchanged against Supabase, Neon, and local docker Postgres, so dev/CI/prod use identical
  code paths and only `DATABASE_URL` changes.
- **US-3 (auth/authz state in Postgres).** As a security owner, I want all users, roles,
  refresh-token rotation/blacklist, and login-attempt state persisted relationally, so F10 can
  enforce JWT rotation, blacklisting, and lockout with durable state.
- **US-4 (versioned schema).** As an ops engineer, I want every schema change expressed as an
  Alembic migration and `alembic upgrade head` to build the whole schema on a fresh DB, so
  environments are reproducible and CI can verify migrations on a throwaway Postgres.
- **US-5 (contract-faithful models).** As a feature author (F1/F17/F9/F4/F13), I want the
  tables to mirror the canonical Pydantic contracts (`DocumentMeta`, `Chunk`, `ChatMessage`,
  `Citation`, `AnswerResponse` fields), so persisted rows round-trip to the shared models
  without ad-hoc mapping.
- **US-6 (local one-command dev).** As a new contributor, I want `make db-up` and
  `make migrate` to stand up Postgres + Redis and apply migrations, so I can run the stack in
  minutes.
- **US-7 (seeded admin).** As an operator, I want a seed script that provisions the admin user
  from env vars, so a fresh deployment has an admin without manual SQL.
- **US-8 (non-blocking).** As a performance owner, I want every DB path to be async and never
  block the event loop, so the persistence layer honors the project-wide async rule.

---

## EARS acceptance criteria

### Engine & session (US-1, US-8)
- **AC-1.1** The system shall expose a single async SQLAlchemy engine built on the asyncpg
  driver, constructed once at process startup and reused for the process lifetime.
- **AC-1.2** The system shall size the connection pool from Settings with `pool_size` defaulting
  to `≤ 5` and shall document the `max_overflow` behaviour for free-tier limits.
- **AC-1.3** The system shall expose one `get_session` FastAPI dependency that yields an
  `AsyncSession` and guarantees the session is closed (and rolled back on unhandled exception)
  when the request scope ends.
- **AC-1.4** WHEN the configured URL targets a pgbouncer/transaction-pooling endpoint (Supabase
  session-pooler), the system shall set asyncpg `statement_cache_size=0` and this caveat shall
  be documented in `design.md`.
- **AC-1.5** The system shall use only async SQLAlchemy sessions for all DB access; the sync
  Session/engine API shall not appear anywhere in `backend/app/` (enforced by the project ruff/grep CI check).
- **AC-1.6** IF a pure-CPU inline exception applies (e.g. none for F12 — all DB work is async
  I/O), THEN `design.md` shall state explicitly that F12 introduces no `to_thread`/executor
  offload because it performs no CPU-bound work.

### Portability (US-2)
- **AC-2.1** WHEN `DATABASE_URL` points at Supabase, Neon, or local docker Postgres, the app
  shall boot and pass its CRUD smoke tests without code changes.
- **AC-2.2** The system shall read the database URL exclusively from the central Pydantic
  `Settings` (`DATABASE_URL`, asyncpg scheme) and shall not hard-code any connection string.

### Models mirror contracts (US-3, US-5)
- **AC-3.1** The system shall define SQLAlchemy 2.0 typed (`Mapped[...]` / `mapped_column`)
  models for exactly the twelve Shared-Context tables and no others.
- **AC-3.2** The `documents` model shall mirror every `DocumentMeta` field and add a `status`
  enum (`registered|downloaded|extracted|indexed|failed`).
- **AC-3.3** The `chunks` model shall mirror every `Chunk` field, hold an FK to `documents`, and
  carry an index on `(doc_id, seq)`.
- **AC-3.4** The `messages` model shall mirror `ChatMessage` (role, content, token_count,
  citations, refused, created_at), store `citations` as JSONB nullable, hold an FK to
  `sessions`, and carry an index on `(session_id, created_at)`.
- **AC-3.5** The `sessions` model shall carry `total_tokens`, `summary` (nullable),
  `summary_token_count` (nullable), `summarized_upto_message_id` (nullable FK to `messages`),
  `title`, `user_id` (nullable — anonymous sessions allowed), timestamps, `is_archived`, and an
  index on `(user_id, last_active_at)`.
- **AC-3.6** The `refresh_tokens` model shall carry a unique `jti`, `issued_at`, `expires_at`,
  nullable `revoked_at`, nullable `replaced_by_jti`, and optional `user_agent`/`ip`; WHERE a row
  exists with `revoked_at IS NULL` and `expires_at` in the future, that jti shall be treated as
  valid (this table IS the blacklist).
- **AC-3.7** The `login_attempts` model shall record `email_or_ip`, `attempted_at`, and
  `success`, supporting a windowed count query for lockout (10 failures / 15 min, enforced by F10).
- **AC-3.8** The `request_logs` model shall provide columns for every field logged by F13,
  including `pipeline_flags` (JSONB), `cache_hit`, `refused`, `degraded`, `memory_summarized`,
  the per-stage timings (`embed_ms`, `retrieve_ms`, `rerank_ms`, `rewrite_ms`, `memory_ms`,
  `summarize_ms`, `llm_ms`, `total_ms`), `tokens_in`/`tokens_out`, `est_cost_usd`, `model`,
  `http_status`, and `error_type`.
- **AC-3.9** The `cache_entries` model shall store `query_text`, `embedding` as BYTEA
  (float32 ≈ 6 KB for 1536-dim), `answer` JSONB, `index_manifest_id`, `hits`, and hit
  timestamps.
- **AC-3.10** The `eval_runs`/`eval_results` models shall store run metadata (`git_sha`,
  `index_manifest` JSONB, `pipeline_flags` JSONB) and per-metric rows carrying a `slice_tag`
  (e.g. `code_switched`).

### Migrations (US-4)
- **AC-4.1** The system shall initialise Alembic with autogenerate support and the first
  migration shall create all twelve tables, their enums, FKs, and declared indexes.
- **AC-4.2** WHEN `alembic upgrade head` runs against a fresh Supabase/Neon/local DB, all tables
  shall be created with no manual step.
- **AC-4.3** The system shall apply a stable naming convention for constraints/indexes so
  autogenerate produces deterministic, reviewable migrations.
- **AC-4.4** Every future schema change shall be an Alembic migration; no schema change shall be
  applied by raw DDL at runtime.

### Local dev & seed (US-6, US-7)
- **AC-5.1** The system shall provide a docker-compose file bringing up Postgres and Redis, with
  `make db-up` starting them and `make migrate` running `alembic upgrade head`.
- **AC-5.2** The system shall provide an async seed script that creates the admin user from
  `ADMIN_EMAIL`/`ADMIN_PASSWORD` env vars with role `admin`, and IF the admin already exists THEN
  the script shall be idempotent (no duplicate, no crash).

### CI verification (US-2, US-4)
- **AC-6.1** WHEN CI runs, it shall spin up a disposable Postgres service container, run
  `alembic upgrade head`, and execute CRUD smoke tests for every model.
- **AC-6.2** IF `alembic upgrade head` fails or any CRUD smoke test fails, THEN the CI job shall
  fail.

---

## Out of scope (owned elsewhere)
- Token issuance/rotation/lockout **logic** → F10 (F12 only stores the state).
- Sliding-window/summary **logic** and token accounting → F17 (F12 only stores
  `sessions`/`messages` and the summary columns).
- Semantic-cache lookup/similarity **logic** → F9 (F12 only stores `cache_entries`).
- Eval **execution** and metric computation → F4 (F12 only stores `eval_runs`/`eval_results`).
- Request-log **writing** and cost accounting → F13 (F12 only defines `request_logs`).
- Any vector storage → Pinecone (pgvector explicitly not used).

## Definition of done
All acceptance criteria above pass, the three acceptance criteria in the feature card hold
(`alembic upgrade head` builds all tables on a fresh managed DB; CRUD smoke tests pass in CI;
app boots against both Supabase and local docker Postgres), and no fixed stack decision was
changed.
