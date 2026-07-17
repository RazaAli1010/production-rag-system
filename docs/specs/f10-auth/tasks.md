# F10 — Authentication & Authorization · tasks.md

**Module:** `backend/app/auth/` + `backend/app/core/security.py` · **Phase:** C ·
**Depends on:** F12 · **Migrations:** none · **Eval gate:** none (requirements §1.3)

Each task is ≈ ≤ 1 hour and lands green. The order is bottom-up and mirrors F9's: pure crypto first
(everything rests on it), then the DB layer with no FastAPI in sight, then the HTTP edge, then the
CLI, then the matrix, then the feature-card acceptance suite.

**T13 IS the feature.** F10 is not done when the endpoints return 200 — it is done when
`tests/auth/test_acceptance.py` proves the four feature-card criteria. There is no eval-gate task
here and that is deliberate, not an omission: CLAUDE.md's label sequence has no `f10` label, and
auth changes no retrieval, so a delta report would be a table of zeros. **Do not invent one.**

Two traps worth reading before T1:
- `bcrypt==4.0.1` is pinned for a reason (F12's DONE.md). Bumping it breaks every hash with a
  bogus "password cannot be longer than 72 bytes".
- `pytest` truncating `documents`/`chunks` is what produced `false_refusal_rate=1.0` in the f7/f8
  reports. F10's teardown truncates **only its four tables**. Never add the corpus tables to it.

---

### T1 — Settings block + dependencies + test scaffold
Add the `# --- Auth (F10) ---` block from design §9 to `app/core/settings.py` (`JWT_SECRET:
SecretStr` **required, no default**, `JWT_ALGORITHM`, `JWT_LEEWAY_S`, `ACCESS_TOKEN_TTL_MIN`,
`REFRESH_TOKEN_TTL_DAYS`, `BCRYPT_ROUNDS`, `AUTH_EMAIL_DOMAIN_ALLOWLIST`, `LOGIN_MAX_FAILURES`,
`LOGIN_LOCKOUT_WINDOW_MIN`, the four `RATE_LIMIT_*`). Add `pyjwt==2.10.1` and
`python-multipart==0.0.20` to `pyproject.toml` under an `# --- Auth (F10) ---` comment — do **not**
re-pin passlib/bcrypt, F12 already did. Add `JWT_SECRET` to `.env.example`.

Because `JWT_SECRET` is required with no default, **every existing conftest env stub and the CI env
break until they set it** — fix them in this task, not later: `tests/db`, `tests/rag`,
`tests/cache`, `tests/evals`, `tests/ingestion`, `tests/indexing` conftests + every job in
`.github/workflows/ci.yml`. Create `backend/tests/auth/conftest.py` mirroring
`tests/cache/conftest.py` (own engine/session, `lru_cache` reset, autouse env stubs, autouse
`TRUNCATE users, refresh_tokens, login_attempts, api_keys CASCADE`).

**Test:** `tests/auth/test_settings.py` — defaults exactly `JWT_ALGORITHM=="HS256"`,
`JWT_LEEWAY_S==30`, `ACCESS_TOKEN_TTL_MIN==15`, `REFRESH_TOKEN_TTL_DAYS==7`, `BCRYPT_ROUNDS==12`,
`AUTH_EMAIL_DOMAIN_ALLOWLIST==[]`, `LOGIN_MAX_FAILURES==10`, tiers `5/20/60/30`; unsetting
`JWT_SECRET` raises `ValidationError`. Whole suite (`pytest tests/`) still green.

---

### T2 — `app/core/security.py`: bcrypt half
`CryptContext(schemes=["bcrypt"], bcrypt__rounds=settings.BCRYPT_ROUNDS)`, module-level
`_DUMMY_HASH`, `async hash_password`, `async verify_password(pw, hashed: str | None)` — both via
`anyio.to_thread.run_sync`; `hashed=None` verifies against `_DUMMY_HASH` and returns `False`
(design §3). Plus `api_key_hash` (sha256 hex) and `new_api_key` (`crag_` + `token_urlsafe(32)`),
both inline CPU.

**Test:** `tests/auth/test_security.py` — hash→verify round-trip; wrong password `False`;
`verify_password(pw, None)` is `False` and takes ≥ 50% of a real verify's wall time (the timing-oracle
defence, AC-11); `hash_password` twice on the same input gives different hashes (salt);
`api_key_hash` is stable and 64 hex chars; `new_api_key()[0].startswith("crag_")`.

---

### T3 — `app/core/security.py`: JWT half
`encode_access(user_id, role, sid)` → claims `{sub, role, jti, sid, typ:"access", exp, iat}`;
`encode_refresh(user_id, role)` → `(token, jti, expires_at)` with `typ:"refresh"`;
`decode_token(token, *, expect)` → `jwt.decode(..., algorithms=[JWT_ALGORITHM],
leeway=JWT_LEEWAY_S)`, raising `AuthError(401, GENERIC)` on bad signature, expiry, or `typ` mismatch.
All three inline — **no thread pool** (design §3's table). Add `AuthError` + `GENERIC` to
`app/core/exceptions.py` (create it if absent — CLAUDE.md's `core/` layout names it).

**Test:** extend `test_security.py` — round-trip claims; tampered payload raises; token signed with
a different secret raises; a token expired by 29s **passes** on leeway and by 31s **fails** (AC-25);
`decode_token(access, expect="refresh")` raises (AC-21); every raise carries the identical `GENERIC`
detail (AC-28).

---

### T4 — `app/auth/schemas.py` + `seed.py` cleanup
`Principal`, `RegisterRequest` (no `role` field), `TokenResponse`, `UserOut` (no
`hashed_password`) per design §4. Then delete `_hash_password` and the local `CryptContext` from
`app/db/seed.py` and delegate to `core.security.hash_password` — its docstring has promised this
since F12 (AC-18).

**Test:** `UserOut.model_validate(user_orm)` has no hash attribute; `RegisterRequest(role="admin")`
is ignored/rejected by `extra` config (AC-6); `tests/db`'s existing seed test still passes and
`seed.py` no longer imports `passlib`.

---

### T5 — `service.py`: register + authenticate
`register` (domain allowlist check when non-empty; `IntegrityError` → `AuthError(409)`) and
`authenticate` exactly in design §5.1's step order — **verify before the `user is None` branch**,
lockout counted first, `login_attempts` written on both paths.

**Test:** `tests/auth/test_service.py` — register happy; duplicate → 409; allowlist on rejects
`x@gmail.com` and accepts `x@pu.edu.pk`, allowlist empty accepts both; authenticate happy returns a
decodable pair whose access `sid` equals the new `refresh_tokens.jti`; wrong password / unknown
email / inactive user all raise 401 with the **byte-identical** detail; a `login_attempts` row lands
with the right `success` on every path.

---

### T6 — `service.py`: lockout window
`_recent_failures(session, email)` — the `count(*) WHERE email_or_ip=:e AND success=false AND
attempted_at > now() - LOGIN_LOCKOUT_WINDOW_MIN` query that `app/db/models/auth.py`'s docstring
already specifies. Wire it as `authenticate` step 1.

**Test:** `tests/auth/test_lockout.py` — 9 failures then correct password succeeds; 10 failures →
`AuthError(429)`; the 429 path does **not** call bcrypt (monkeypatch `verify_password` to raise);
back-dating the attempt rows past the window restores success with no operator action (AC-14).

---

### T7 — `service.py`: rotate, revoke, resolve
`rotate_refresh` (design §5.4 — `with_for_update()`, revoke + `replaced_by_jti` + insert in one
transaction), `revoke_family` (single UPDATE with `revoked_at IS NULL` in the WHERE → idempotent by
construction), `resolve_jwt` (design §5.2 — the **one** join query; `role` from the row, not the
claim), `resolve_api_key` (design §5.3 — `kind="api_key"` even for an admin owner).

**Test:** extend `test_service.py` — rotate returns a new pair, old row has `revoked_at` +
`replaced_by_jti` = new jti; reusing the old refresh → 401 (AC-20); an access token posted to rotate
→ 401; two concurrent rotations of one token → exactly one 401 (the `FOR UPDATE` proof);
`revoke_family` twice keeps the first `revoked_at`; `resolve_jwt` after `revoke_family` → 401
(AC-23); demoting a user to student in the DB makes their existing admin-claim token resolve as
student (the "row is truth" proof); `resolve_api_key` on a revoked key → 401; an API key owned by
the admin user yields `kind == "api_key"`, not `"admin"`.

---

### T8 — `app/auth/deps.py` + `app/main.py`
The four deps + `rate_tier` per design §6. Then `app/main.py` — the repo's first FastAPI app:
`FastAPI(title="CampusRAG")`, the `AuthError` handler (adding `WWW-Authenticate: Bearer` on 401s),
mount `api.auth` and `api.internal`. Keep it ~20 lines; F11 owns middleware, CORS, and lifespan.

**Test:** `rate_tier` is a pure unit test over all four principal kinds → exact `(key, limit)`
tuples from Settings (AC-35); `get_current_user_optional` with no headers returns `None` **without
a DB query** (assert via a session spy); with both headers present, the JWT wins (AC-32); a bad
token on the optional path → 401, not `None` (AC-27).

---

### T9 — `app/api/auth.py`
The five endpoints. `/token` takes `OAuth2PasswordRequestForm = Depends()` (this is where
`python-multipart` earns its pin). `/logout` reads `sid` from the access token → `revoke_family` →
`204`. Docstring on `/api/ask`-adjacent behaviour: **tokens are validated at request start only** —
an SSE stream outliving its `exp` is not killed (requirements §5).

**Test:** `tests/auth/test_api_auth.py` with `httpx.ASGITransport` — each endpoint's status codes;
no response body anywhere contains `hashed_password` or a bcrypt prefix; 401s carry
`WWW-Authenticate`; `/me` unauthenticated → 401; `/me` with an API key returns the owning user.

---

### T10 — `app/api/internal.py` + the matrix
`GET /internal/ping` guarded by `require_role("admin")`, returning `{"ok": true}` — six lines whose
only job is to be the mount point F13 hangs stats/cache-flush/doc-status/eval-results off, and to
make the matrix testable today. Do **not** build those four endpoints (requirements §6).

**Test:** `tests/auth/test_matrix.py` — parametrized over all **16** cells of requirements §3.6:
{anonymous, student, admin, api_key} × {ask-path principal resolves, rate tier value, history
allowed, `/internal/ping` status}. Student → 403, admin → 200, api_key → 403, anonymous → 401
(AC-33/34/36).

---

### T11 — `app/auth/run.py` (CLI)
`--prune` (both deletes, print counts), `--issue-key --email --label` (print the plaintext **once**,
store only sha256), `--revoke-key --label`. `asyncio.run` entrypoint, matching
`app/caching/run.py`'s shape.

**Test:** `tests/auth/test_run.py` — prune deletes expired refresh rows + old attempts and leaves
live ones, returning correct counts; issued key authenticates via `resolve_api_key` and the raw key
is **absent** from `api_keys` (only its hash is stored); `--revoke-key` sets `revoked_at` and the
key then 401s.

---

### T12 — CI job + the two guards
Add an `auth:` job to `.github/workflows/ci.yml` mirroring `caching:` (Postgres service → `alembic
upgrade head` → `pytest tests/auth` → ruff). Extend the async-guard grep to `app/auth/`, and add
the new grep: no `CryptContext`, `.hash(`, or `.verify(` outside `app/core/security.py` (AC-15).

**Test:** `tests/auth/test_no_new_migration.py` — after `upgrade head`, `alembic revision
--autogenerate` yields an **empty diff**, proving F10 added no schema (AC-46). Plus
`tests/auth/test_logging.py`: run register→token→refresh→logout against a captured structlog stream
and assert no token, key, password, or hash substring appears in any event (AC-29). Plus
`tests/auth/test_swagger.py`: `/openapi.json` declares `OAuth2PasswordBearer` with
`tokenUrl == "api/auth/token"` and an `APIKeyHeader` named `X-API-Key` (AC-39).

---

### T13 — Acceptance suite (**the definition of done**)
`tests/auth/test_acceptance.py`, one test per feature-card criterion (requirements §4):

1. **`test_full_flow`** — register → token → authed principal resolves on the ask path → refresh →
   logout → old refresh rejected **and** the pre-logout access token rejected (AC-23).
2. **`test_roles`** — student blocked from `/internal/*`, admin passes, API key limited to ask
   (delegates to T10's matrix).
3. **`test_swagger_authorize_end_to_end`** — drive the real password grant as Swagger does
   (`POST /api/auth/token` as `application/x-www-form-urlencoded`, then `GET /api/auth/me` with the
   returned bearer) → `200`. Manually confirm the Authorize button at `/docs` once and record it in
   `DONE.md` — a browser click is not automatable here and saying so is better than pretending.
4. **`test_concurrency`** — 8 concurrent `/token` requests complete in `< 8 × single_hash × 0.6`
   (AC-17: bcrypt does not serialize the event loop).

Then write `docs/specs/f10-auth/DONE.md` in F12's format: what was built, each feature-card
criterion with its evidence, anything deferred (state it plainly — the Swagger button click, any
un-run CI job), and follow-ups. **Explicitly record that F10 has no eval gate and why**, so the
next person reading the label sequence does not think one was skipped by accident.

---

## Task → acceptance criteria map

| Task | ACs |
|---|---|
| T1 | 47 |
| T2 | 11, 15, 16 |
| T3 | 8, 21, 25 (leeway), 28 |
| T4 | 1, 6, 18 |
| T5 | 1–6, 7, 9, 10, 11, 12 |
| T6 | 13, 14 |
| T7 | 19, 20, 21, 22, 23, 24, 25, 30, 31 |
| T8 | 26, 27, 32, 35 |
| T9 | 7, 37, 38 |
| T10 | 33, 34, 36 |
| T11 | 41, 42, 43, 44 |
| T12 | 15, 29, 39, 45, 46 |
| T13 | 17, 40 + feature card §4 |
