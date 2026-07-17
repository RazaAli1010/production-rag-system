# F10 — Authentication & Authorization · DONE

Status: **code complete, verified end-to-end against a live server and a real Postgres.** All four
feature-card acceptance criteria pass. One criterion (the Swagger *button click*) is verified
manually rather than automatically — stated plainly below rather than implied by the automated test
that drives the same grant over HTTP.

## What was built

`backend/app/auth/` — `schemas.py`, `service.py`, `deps.py`, `run.py`.
`backend/app/core/` — `security.py` (bcrypt + JWT + api-key hashing), `exceptions.py`.
`backend/app/api/` — `auth.py` (5 endpoints), `internal.py` (F13's admin-gated mount point).
`backend/app/main.py` — **the repo's first FastAPI app.**
Plus `backend/tests/conftest.py` (new root), `backend/tests/auth/` (11 modules, **116 tests**),
the `auth:` CI job with two new guards, and the Settings/pyproject/.env.example changes.

**116 new tests; full suite 620 passed.** `ruff check` clean across every new module.

## Feature-card acceptance criteria

1. **Flow test: register → token → authed /ask → refresh → logout → old refresh rejected.**
   `tests/auth/test_acceptance.py::test_ac1_full_flow`, plus the same flow driven by curl against a
   live `uvicorn app.main:app`: register 201 → duplicate 409 → token (form-encoded) → `/me` 200 →
   logout 204 → **old access token 401** → old refresh 401.
   The `/ask` leg is the *principal* rather than the endpoint: `/api/ask` is F11's, and F10's
   contribution to it is `get_current_user_optional` + `rate_tier()`. `/api/auth/me` stands in as
   the authed surface. This is scope, not a gap — F11 mounts the route.

2. **Role tests: student blocked from /internal/*; admin passes; API key limited to ask.**
   `test_matrix.py` covers all 16 cells (4 actors × {internal, authed surface, rate tier, history});
   `test_acceptance.py::test_ac2_roles` asserts the criterion directly. Confirmed live: student
   403, admin 200, anonymous 401, and — the one that matters — **the admin's own API key gets 403**,
   because `kind` is checked, not `role`.

3. **Swagger Authorize flow works end-to-end.**
   `test_swagger.py` asserts `/openapi.json` declares `OAuth2PasswordBearer` at
   `tokenUrl: api/auth/token` and `APIKeyHeader: X-API-Key`;
   `test_acceptance.py::test_ac3_swagger_authorize_grant` reads the advertised `tokenUrl` out of the
   live OpenAPI document and drives the real `application/x-www-form-urlencoded` password grant
   against it, then makes a bearer call.
   **Manually verified:** `/docs` → Authorize → `GET /api/auth/me` → 200, against
   `uvicorn app.main:app --port 8077`. The button click itself is a browser action and is not
   automated here.

4. **Bcrypt + blacklist + lockout unit-tested.**
   Bcrypt: `test_ac4_bcrypt_does_not_serialize_the_event_loop` — 8 concurrent hashes at cost 10
   finish in well under 8× a single hash, proving `anyio.to_thread.run_sync` keeps it off the loop.
   Blacklist: `test_ac4_blacklist` — `revoke_family` kills both the refresh token and the access
   token minted from it. Lockout: `test_ac4_lockout` + `test_lockout.py` (9 fails OK, 10 → 429,
   429 costs no bcrypt, window ages out with no operator action, per-email not per-IP).

## Why there is no eval gate (deliberate, not skipped)

CLAUDE.md's fixed label sequence is `baseline → f5-hybrid-after → … → f9-cache-after →
f17-memory-after`. **There is no `f10` label.** F10 changes no retrieval, no prompt, no generation,
and no cache behaviour — every metric the F4 harness measures is provably identical before and
after, so a delta report would be a table of zeros bought with real OpenAI credit. F10's definition
of done is its acceptance suite instead. `f17-memory-after` is unaffected: the harness runs
`session_id=None` and never constructs a `Principal`.

## Fixed-stack / scope notes

- No fixed stack decision was altered. OAuth2 password flow + JWT (`pyjwt`) + `passlib[bcrypt]` +
  RBAC, all auth state in Postgres; Redis untouched by F10.
- **Zero Alembic migrations.** F12's `0001_initial` already shipped every column needed.
  `tests/auth/test_no_new_migration.py` asserts `alembic check` reports no pending operations, that
  head is still `0003`, and that `api_keys` has no `scope` column — so the claim cannot rot.
- Out of scope and not built: rate-limit enforcement (F11), `/api/ask` (F11), history endpoints
  (F17), `request_logs.user_id` writing (F13), the real `/internal/*` endpoints (F13), password
  reset / email verification / social login (v2), refresh-reuse chain revocation (v2 — the
  `replaced_by_jti` chain is recorded so it stays a query, not a migration).

## Decisions worth knowing about

- **Access tokens carry `sid`** (the refresh family's jti). `resolve_jwt` joins `refresh_tokens ON
  jti = sid AND revoked_at IS NULL` — that join *is* the blacklist check CLAUDE.md mandates, since
  an access token's own jti is never in that table. Without it `/logout` would be theater: the
  access token would keep working for up to 15 minutes. Verified live (step 9 of the curl flow).
- **API keys are sha256, not bcrypt.** A 32-byte `token_urlsafe` has no low entropy to protect;
  bcrypt would buy zero security while forcing a full table scan on every bot request (a bcrypt
  hash cannot be looked up by equality). Passwords remain bcrypt cost 12.
- **`kind` is separate from `role`** on `Principal`, so an API key owned by the admin still resolves
  as `kind="api_key"` and stays ask-only. Verified live.
- **`role` is read from the `users` row, not the JWT claim** — a demoted admin loses admin
  immediately rather than at token expiry (`test_role_comes_from_the_row_not_the_claim`).
- **`rotate_refresh` uses `SELECT … FOR UPDATE`**; `test_concurrent_rotation_yields_exactly_one_winner`
  proves two simultaneous refreshes yield one 200 and one 401 rather than two valid families.
- **Login lockout is keyed on email, not IP** — IP-keying would let one hostile client lock out
  every account behind a university/carrier NAT.
- **`/logout` is idempotent by its WHERE clause** (`revoked_at IS NULL`), not by a read-then-write,
  so a repeat logout is a 204 no-op that cannot overwrite the original `revoked_at` (AC-24).

## Issues found and fixed during implementation

- **`JWT_SECRET` required with no default breaks 39 test files and every `alembic` invocation**,
  because `settings = Settings()` is module-level. Solved with a new root
  `backend/tests/conftest.py` doing `os.environ.setdefault("JWT_SECRET", ...)` — `Settings(_env_file=None)`
  still reads `os.environ`, so all 39 files work unchanged. Only the 6 CI env blocks needed the
  variable added. **8 files touched instead of 45**, with the security property intact.
- **passlib deprecates per-call `rounds=`** (`CryptContext.hash(pw, rounds=N)` warns and breaks in
  passlib 2.0). Rounds now live on a `@lru_cache`d per-cost `CryptContext`, which is also what
  design §3/AC-16 specified.
- **`/logout` originally read `request.headers["authorization"]` directly** → `KeyError` → 500 for
  an API-key caller. Replaced with an `access_claims` dependency: a bot has no session to log out
  and now gets a clean 401.
- **The local `backend/.env` had no `JWT_SECRET`**, so `uvicorn app.main:app` refused to boot. That
  is the required-secret design working as intended (fail at deploy, not at first login); a
  generated value was added to the gitignored `.env`.

## Pre-existing failures (NOT caused by F10)

`tests/evals/test_acceptance.py::test_ac1_full_baseline_report` and
`tests/evals/test_ragas.py::test_confirm_true_offloads_and_emits_four_metrics` fail with
`TypeError: <lambda>() takes 3 positional arguments but 4 were given` from
`anyio/_backends/_asyncio.py`. **Confirmed pre-existing**: both fail identically on a clean tree
with F10 stashed. Looks like anyio version drift in a test-local lambda passed to
`to_thread.run_sync`. Out of F10's scope; flagged for whoever owns F4.

## Follow-ups (tracked, not blocking)

1. Push and confirm the new `auth:` CI job goes green on GitHub Actions (mirrors the commands run
   locally; never yet exercised by a real push — same caveat F12's DONE.md records).
2. Fix the two pre-existing `tests/evals` anyio failures above.
3. F11 wires `rate_tier()` to a Redis limiter and mounts `/api/ask` behind
   `get_current_user_optional`. F10 deliberately stops at the tuple.
4. F15 schedules `python -m app.auth.run --prune` (F10 ships the command, not the cron).
5. Set a real `JWT_SECRET` in the Render/prod environment — rotating it invalidates every issued
   token, which is the intended kill switch.
