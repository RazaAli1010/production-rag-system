# F12 — Persistence Layer · DONE

Status: **code complete, locally verified end-to-end.** Two DoD checks from the feature card are
deferred per an explicit decision made during planning (managed-Postgres run) — see below.

## What was built

`backend/app/db/` — `base.py`, `types.py`, `enums.py`, `engine.py`, `session.py`, `seed.py`,
6 model modules (12 tables), Alembic (`env.py` + curated `0001_initial`). Plus
`backend/app/core/settings.py`, `backend/pyproject.toml`, `docker/docker-compose.yml`,
`Makefile`, `.github/workflows/ci.yml`, and `backend/tests/db/` (24 tests).

## Feature-card acceptance criteria

1. **`alembic upgrade head` on a fresh managed DB creates all tables.**
   Docker was not available in the build environment, so this was verified against a **local
   native PostgreSQL 17** instance instead of Docker or a managed Supabase/Neon instance (role
   `raza`, database `campus_rag_test`, created for this verification). `alembic upgrade head`
   created all 12 tables + 4 enums + all indexes with no manual step; `alembic check` reported
   "No new upgrade operations detected" (empty autogenerate diff, confirming the hand-curated
   `0001_initial` migration matches the SQLAlchemy models exactly, AC-4.3); `alembic downgrade
   base` dropped everything cleanly and re-upgrading to head succeeded again.
   **Deferred:** the real Supabase/Neon run. `docker/docker-compose.yml` (postgres:16) is
   written and ready but untested locally (no Docker on this machine). **Follow-up:** run
   `make db-up && make migrate` against Docker, and `alembic upgrade head` once against a real
   Supabase/Neon `DATABASE_URL` (with `DB_STATEMENT_CACHE_SIZE=0` for the session pooler).

2. **CRUD smoke tests pass in CI.**
   All 24 tests in `backend/tests/db` pass locally (twice in a row, to rule out flakiness) against
   the native Postgres 17 instance, covering every task's acceptance criterion (T-1 through T-11):
   Settings validation, engine connectivity + pgbouncer connect-args, `get_session` commit/rollback
   contract, full CRUD + uniqueness/cascade checks for all 12 models, JSONB round-trips, BYTEA
   embedding byte-identity, and the migration upgrade/check/downgrade cycle. `ruff check` is clean
   and the async-guard grep (no sync SQLAlchemy `Session`/`create_engine`, no `requests`, no sync
   `redis` under `app/db/`) passes.
   **Not yet verified:** an actual GitHub Actions run (`.github/workflows/ci.yml` has not been
   exercised by a push/PR from this session). The workflow mirrors the exact commands run locally
   (`alembic upgrade head` → `pytest tests/db` → ruff/grep guard), so it is expected to pass, but
   this is stated explicitly rather than assumed. **Follow-up:** push this branch and confirm the
   `db` job goes green.

3. **App boots against both Supabase and local docker Postgres with only `DATABASE_URL` changed.**
   Verified the "only `DATABASE_URL` changes" portability principle against local native Postgres
   (swapping `.env`'s `DATABASE_URL` was the only change needed to move from the default/example
   value to the working local instance). **Deferred:** the Docker Postgres and Supabase/Neon legs
   specifically, for the same reason as (1).

## Fixed-stack / scope notes

- No fixed stack decision was altered. pgvector remains unused (Pinecone owns vectors;
  `cache_entries.embedding` is BYTEA, compared in-process by F9 — not implemented here).
- Out-of-scope items (F10 token logic, F17 memory logic, F9 cache logic, F4 eval execution, F13
  request-log writing) were not implemented, per requirements.md.

## Issues found and fixed during verification (worth knowing about)

- **`passlib==1.7.4` is incompatible with `bcrypt>=4.1`** (bcrypt removed the `__about__.__version__`
  attribute passlib's backend detection reads, causing a spurious "password cannot be longer than
  72 bytes" error on every hash call). Pinned `bcrypt==4.0.1` explicitly in `pyproject.toml`
  alongside `passlib[bcrypt]==1.7.4` to fix. F10 should keep this pin in mind if it touches auth
  hashing.
- **pytest-asyncio + a process-lifetime `@lru_cache`d engine don't mix in tests**: `get_engine()`/
  `get_sessionmaker()` are correctly cached for the process lifetime in production (one ASGI
  process, one event loop), but pytest-asyncio gives each test function its own event loop, and
  asyncpg connections are loop-bound. `backend/tests/db/conftest.py` has an autouse fixture that
  clears the cache and disposes the pool after every test to keep the test suite deterministic.
  This is a test-harness-only concern; `engine.py`/`session.py` themselves are unchanged from
  design.md.
- Two test assertions initially false-failed on cascade deletes because `session.get()` returned a
  stale object from the SQLAlchemy identity map instead of re-querying; fixed by passing
  `populate_existing=True` in those assertions (`test_models_user_auth.py`,
  `test_models_corpus.py`, `test_models_evals.py`). The cascades themselves were correct — this was
  a test bug, not a schema bug.

## Follow-ups (tracked, not blocking)

1. Run `make db-up && make migrate` against the Docker Postgres in `docker/docker-compose.yml`.
2. Run `alembic upgrade head` once against a real Supabase/Neon `DATABASE_URL`.
3. Push and confirm `.github/workflows/ci.yml`'s `db` job passes on GitHub Actions.
