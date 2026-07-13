# F12 — Persistence Layer · tasks.md

Ordered, each task ≤ ~1h, each with a test/verification criterion. F12 is a **Phase A**
foundation feature, so there is **no eval-gate task** (the eval gate applies only to Phase B/C
enhancements F5–F9). Definition of done = the feature-card acceptance criteria (T-14).

---

### T-1 · Settings keys
Add the DB keys from design §6 to the central `Settings` class (`DATABASE_URL`, `DB_POOL_SIZE`,
`DB_MAX_OVERFLOW`, `DB_POOL_TIMEOUT`, `DB_POOL_RECYCLE`, `DB_STATEMENT_CACHE_SIZE`, `DB_ECHO`,
`ADMIN_EMAIL`, `ADMIN_PASSWORD`). Provide `.env.example` entries.
**Test:** `Settings()` loads from a sample `.env`; missing `DATABASE_URL` raises a validation
error; defaults match the table.

### T-2 · Base, metadata naming convention, shared typed columns
Create `base.py` (`Base` + `MetaData(naming_convention=...)`) and `types.py`
(`UUIDpk`, `TZDateTime`, `CreatedAt`, `JSONBDict`) and `enums.py`
(`UserRole`, `DocumentStatus`, `MessageRole`, `RequestChannel`).
**Test:** import succeeds; `Base.metadata.naming_convention` contains the five keys; enums have
the exact members from the spec.

### T-3 · Async engine & sessionmaker
Implement `engine.py`: `get_engine()` (asyncpg, `pool_size`/`max_overflow`/`pool_timeout`/
`pool_recycle`/`pool_pre_ping`, `_connect_args()` sets `statement_cache_size=0` when
`DB_STATEMENT_CACHE_SIZE==0`), `get_sessionmaker()`.
**Test:** against local docker Postgres, `SELECT 1` via a session returns 1; with
`DB_STATEMENT_CACHE_SIZE=0` the connect args include `statement_cache_size=0`.

### T-4 · `get_session` dependency
Implement `session.py` async-generator dependency (yield → commit; except → rollback → raise;
close via `async with`).
**Test:** a dummy FastAPI route using `Depends(get_session)` commits on success; a route that
raises leaves no partial row (rollback verified).

### T-5 · User & auth models
Add `user.py` (`User`, `ApiKey`) and `auth.py` (`RefreshToken`, `LoginAttempt`) per design §3.1–3.2,
including unique `email`, unique `jti`, and `(email_or_ip, attempted_at)` index.
**Test:** create/read/update/delete each model against local Postgres; duplicate `email` and
duplicate `jti` raise `IntegrityError`.

### T-6 · Corpus models (mirror `DocumentMeta`/`Chunk`)
Add `corpus.py` (`Document` with `status` enum + CHECKs on `source_org`/`file_type`; `Chunk` with
FK → documents and `(doc_id, seq)` index).
**Test:** insert a `Document` + child `Chunk`; assert every `DocumentMeta`/`Chunk` field is
present and column types match; cascade delete removes chunks.

### T-7 · Chat models (F17 state) incl. circular FK
Add `chat.py` (`Session`, `Message`) per design §3.4 with `(session_id, created_at)` and
`(user_id, last_active_at)` indexes and the `summarized_upto_message_id` FK via `use_alter=True`.
**Test:** insert session + messages; set `summarized_upto_message_id`; nullable `user_id`
(anonymous) insert succeeds; `citations` JSONB round-trips a `list[Citation]` dict.

### T-8 · Ops models (`request_logs`, `cache_entries`)
Add `ops.py` per design §3.5 — all F13 timing/flag/cost columns and the F9 cache columns
(`embedding` BYTEA, `answer` JSONB).
**Test:** insert a `RequestLog` populating every stage-timing column and `pipeline_flags` JSONB;
insert a `CacheEntry` with a 1536-float32 `embedding` (~6 KB) and read it back byte-identical.

### T-9 · Eval models
Add `evals.py` (`EvalRun`, `EvalResult` with `slice_tag`, FK cascade).
**Test:** insert a run + several results with distinct `metric`/`slice_tag`; cascade delete of the
run removes results.

### T-10 · Register models for autogenerate
Ensure `models/__init__.py` imports every model so `Base.metadata` is complete.
**Test:** `len(Base.metadata.tables) == 12` and the table names match the Shared-Context list
exactly.

### T-11 · Alembic init + `0001_initial`
Initialise async Alembic; wire `env.py` `target_metadata = Base.metadata` and async online
migrations; generate/curate `0001_initial` (enums → tables → post-create circular FK → indexes;
reverse downgrade).
**Test:** on a fresh DB, `alembic upgrade head` creates all 12 tables + enums + indexes;
`alembic downgrade base` drops them cleanly; a re-run autogenerate produces an **empty** diff
(schema matches models).

### T-12 · docker-compose + Makefile + seed
Add `docker/docker-compose.yml` (Postgres 16 + Redis 7, healthchecks) and `Makefile`
(`db-up`, `migrate`, `db-down`, `seed`); implement idempotent `seed.py::seed_admin()` from
`ADMIN_EMAIL`/`ADMIN_PASSWORD`.
**Test:** `make db-up && make migrate && make seed` on a clean machine yields all tables + one
admin user; running `seed` twice does not duplicate or error.

### T-13 · CI wiring (Postgres service container)
Add the CI job: Postgres service container → `alembic upgrade head` → `pytest backend/tests/db`
(CRUD smoke for all 12 models) → ruff/grep async-guard over `backend/app/db/` (no sync
SQLAlchemy/`requests`/blocking Redis).
**Test:** CI passes green on a correct branch; deleting a migration or introducing a sync
`Session` import makes the job fail.

### T-14 · Definition of done (feature-card acceptance)
Verify all three card criteria and mark the feature done:
1. `alembic upgrade head` on a **fresh Supabase/Neon** DB creates all tables (run once against a
   real managed instance, not just the CI container).
2. CRUD smoke tests for every model pass in CI (T-13).
3. The app boots against **both** Supabase (session-pooler URL, `DB_STATEMENT_CACHE_SIZE=0`) and
   local docker Postgres with only `DATABASE_URL` changed.
**Test:** a short `docs/specs/f12-persistence/DONE.md` note records the Supabase/Neon upgrade
output and the two boot checks; no fixed stack decision was altered; pgvector-not-used recorded.

---

**Not in this feature (do not implement here):** token rotation/lockout logic (F10), memory
window/summary logic (F17), cache lookup/similarity (F9), eval execution (F4), request-log
writing/cost math (F13). F12 delivers only the schema, engine/session seam, and migrations these
features build on.
