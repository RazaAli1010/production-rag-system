# F10 — Authentication & Authorization · requirements.md

**Module:** `backend/app/auth/` + `backend/app/core/security.py` · **Phase:** C (production layer) ·
**Depends on:** F12 (`users`, `api_keys`, `refresh_tokens`, `login_attempts` — all already shipped) ·
**Blocks:** F11 final form, F14 auth UI, F16 Telegram bot · **Flag:** none (see §1.2) ·
**Eval gate:** none (see §1.3)

---

## 1. Overview

Production auth the way job descriptions expect it: OAuth2 password flow, JWT access+refresh,
bcrypt hashing, student/admin RBAC, API keys for bots — with **every piece of auth state in
Postgres**. Redis is rate limiting and the F9 cache only; it is never the source of truth for who
someone is.

The product constraint that shapes everything: **the ask flow stays zero-friction**. A student on
mobile data must be able to ask "probation se kaise nikalta hoon" without an account. Auth buys you
history, a higher rate tier, and the admin surface — it is never the price of admission.

### 1.1 Design decisions resolved here (do NOT re-derive)

- **Zero migrations.** F12 shipped `users`, `api_keys`, `refresh_tokens`, `login_attempts` with the
  exact columns this feature needs, including `refresh_tokens.replaced_by_jti` for the rotation
  chain and the `(email_or_ip, attempted_at)` index for the lockout window. F10 adds **no columns
  and no tables**. The one thing that would force a migration — an `api_keys.scope` column — is not
  needed, because there is exactly one scope (ask-only) and one scope is a hardcoded constant, not a
  schema field.
- **F10 is the first HTTP surface.** `backend/app/api/` and `backend/app/main.py` do not exist yet;
  F3's chain is invoked in-process by the F4 harness. F10 creates the FastAPI app and the
  `/api/auth/*` router. It does **not** create `/api/ask` (F11 owns that) and does **not** implement
  the Redis rate limiter (F11 owns that either). F10 ships the *inputs* F11 needs: the principal
  dependencies and a pure `rate_tier()` resolver.
- **API keys are sha256, not bcrypt.** Bcrypt exists to make *low-entropy human passwords* expensive
  to brute-force. An API key is 32 bytes of `secrets.token_urlsafe` entropy — there is nothing to
  brute-force, so bcrypt buys zero security and costs a thread-pool hop plus a full table scan on
  every bot request (a bcrypt hash cannot be looked up by equality). sha256 of the key gives an O(1)
  equality lookup and stays in `key_hash` unchanged. Passwords remain bcrypt.
- **Access tokens carry `sid`, the refresh family's jti.** Without it, `/logout` is theater: the
  refresh token dies but the already-issued access token keeps working for up to 15 minutes. With
  it, `get_current_user`'s existing user-load query joins `refresh_tokens` on `sid` and a logged-out
  session is dead immediately. This costs one JOIN on a query already being made, and it is what
  makes the feature brief's "blacklist check inside `get_current_user`" literally true — an access
  token's own jti is never in `refresh_tokens`.
- **Access tokens are validated at request start only.** An SSE stream that outlives its token's
  `exp` is not killed mid-answer. Documented, tested, not fixed — see §5.
- **API key issuance is a CLI, not an endpoint.** The feature brief lists five endpoints and
  issuance is not among them. The consumer is the F16 Telegram bot, provisioned once by an operator.
  `python -m app.auth.run --issue-key` covers it; an admin-only issuance endpoint gets built when
  something actually needs to issue keys over HTTP.

### 1.2 Why there is no feature flag

CLAUDE.md requires every *enhancement* to be toggleable so A/B and prod rollback work. F10 is not an
enhancement to the retrieval pipeline — there is no "auth off" A/B to run, and a flag that disables
authentication is a vulnerability with a config key. The zero-friction requirement is met by design
(`get_current_user_optional` returns `None` for anonymous), not by a flag. The one genuinely optional
policy — the `*.edu.pk` registration allowlist — **is** config-gated and defaults off
(`AUTH_EMAIL_DOMAIN_ALLOWLIST`).

### 1.3 Why there is no eval gate

CLAUDE.md's fixed label sequence is `baseline → f5-hybrid-after → f6-rerank-after →
f7-rewrite-after → f8-compression-after → f9-cache-after → f17-memory-after`. **There is no `f10`
label**, by design: F10 changes no retrieval, no prompt, no generation, and no cache behaviour, so
every metric the F4 harness measures is provably identical before and after. Running the harness
would produce a delta report of zeros and burn OpenAI credit to prove nothing.

F10's definition of done is its acceptance test suite (§4), not `docs/eval_results/`. The next label
in the sequence, `f17-memory-after`, is unaffected — the harness runs `session_id=None` and never
constructs a principal.

## 2. User stories

**US-1 (Student, anonymous):** As a student who just wants the probation rule and does not want an
account, I want to ask without registering, so the tool is worth opening once.

**US-2 (Student):** As a student who asks a lot, I want an account that gives me a higher rate limit
and my own history, so signing up buys me something concrete.

**US-3 (Student):** As a student on a flaky mobile connection, I want my session to survive for days
without re-entering my password, so I am not re-typing credentials on 3G every 15 minutes.

**US-4 (Student):** As a student who logs out on a shared lab machine, I want my session to be dead
the moment I press it, so the next person at that terminal cannot resume it.

**US-5 (Security owner):** As the person responsible for the login endpoint, I want an account
locked after repeated failures, so a stolen email list cannot be password-sprayed.

**US-6 (Security owner):** As the person responsible for the login endpoint, I want a wrong password
and an unknown email to be indistinguishable — in message *and* in response time — so the endpoint
is not a user-enumeration oracle.

**US-7 (Security owner):** As the person paying for the OpenAI account, I want a stolen refresh
token to be usable only until its owner next refreshes, so token theft is detectable and bounded.

**US-8 (Admin):** As an admin, I want `/internal/*` to reject students, so the ops surface is not one
guessed URL away from a student flushing the cache.

**US-9 (Bot operator):** As the operator of the Telegram bot, I want a long-lived API key scoped to
asking only, so the bot cannot register users or reach `/internal/*` even if its key leaks.

**US-10 (Backend dev):** As the dev integrating auth into the ask pipeline, I want one dependency
that returns the principal or `None`, so the anonymous path costs one `if`, not a parallel router.

**US-11 (Ops):** As an operator, I want `refresh_tokens` and `login_attempts` pruned on demand, so
tables that grow with every login do not grow forever now that they are in Postgres and not Redis.

**US-12 (Reviewer/interviewer):** As someone evaluating this project, I want Swagger's Authorize
button to complete a real OAuth2 password flow, so the auth claim is verifiable in ten seconds.

**US-13 (Ops):** As an operator, I want a burst of concurrent logins to not freeze the API, so
bcrypt — deliberately slow, CPU-bound work — cannot stall the event loop that every other request
shares.

## 3. EARS acceptance criteria

### 3.1 Registration

- **AC-1 (Event-driven):** When a `POST /api/auth/register` arrives with a valid email and a
  password of ≥ 8 characters, the system shall create a `users` row with `role='student'`,
  `is_active=true`, and a bcrypt hash in `hashed_password`, and shall respond `201` with the
  profile — never with the password or its hash.
- **AC-2 (Unwanted):** If the email already exists in `users`, the system shall respond `409` and
  shall not modify the existing row.
- **AC-3 (Unwanted):** If the password is shorter than 8 characters or the email fails
  `EmailStr` validation, the system shall respond `422` and shall not call bcrypt.
- **AC-4 (State-driven):** While `AUTH_EMAIL_DOMAIN_ALLOWLIST` is non-empty, the system shall reject
  with `403` any registration whose email domain does not match a listed suffix.
- **AC-5 (State-driven):** While `AUTH_EMAIL_DOMAIN_ALLOWLIST` is empty (**the default**), the
  system shall accept any valid email domain.
- **AC-6 (Ubiquitous):** The system shall never allow `role` to be set by the request body — the
  registration schema shall have no `role` field, and admin is provisioned only by F12's
  `seed_admin()` or a direct DB operation.

### 3.2 Login (`POST /api/auth/token`)

- **AC-7 (Event-driven):** When credentials submitted as an `OAuth2PasswordRequestForm` match an
  active user, the system shall insert a `refresh_tokens` row and respond
  `{access_token, refresh_token, token_type: "bearer"}`.
- **AC-8 (Ubiquitous):** The access token shall be JWT HS256 with claims `sub` (user id), `role`,
  `jti`, `sid` (the refresh row's jti), `typ: "access"`, and `exp` at now + `ACCESS_TOKEN_TTL_MIN`
  (15). The refresh token shall carry `sub`, `role`, `jti`, `typ: "refresh"`, and `exp` at now +
  `REFRESH_TOKEN_TTL_DAYS` (7). Both shall be signed with `JWT_SECRET`.
- **AC-9 (Ubiquitous):** Every login attempt, successful or not, shall insert a `login_attempts` row
  keyed on the submitted email with the correct `success` value.
- **AC-10 (Unwanted — wrong password):** If the password does not verify, the system shall respond
  `401` with the generic detail `"Incorrect email or password"`.
- **AC-11 (Unwanted — unknown email):** If the email is not in `users`, the system shall respond
  `401` with **the byte-identical body of AC-10**, and shall perform a bcrypt verify against a
  fixed dummy hash so the response time is indistinguishable from AC-10 (US-6).
- **AC-12 (Unwanted — inactive user):** If `users.is_active` is false, the system shall respond
  `401` with the same generic detail.
- **AC-13 (Unwanted — lockout):** If `count(login_attempts WHERE email_or_ip = :email AND
  success = false AND attempted_at > now() - LOGIN_LOCKOUT_WINDOW_MIN)` is ≥ `LOGIN_MAX_FAILURES`
  (10 / 15 min), the system shall respond `429` and shall not call bcrypt or issue a token.
- **AC-14 (Event-driven — lockout clears):** When the newest failure ages past the window, the
  system shall accept a correct password again without operator action — the count is a window over
  `login_attempts`, never a stored `locked_until` flag.

### 3.3 Hashing (async safety, US-13)

- **AC-15 (Ubiquitous):** Every bcrypt `hash()` and `verify()` call in `app/` shall execute inside
  `anyio.to_thread.run_sync`. A grep guard in CI shall fail on a direct `CryptContext.hash(` /
  `.verify(` call outside `app/core/security.py`.
- **AC-16 (Ubiquitous):** Bcrypt cost shall be `BCRYPT_ROUNDS` (12), set explicitly on the
  `CryptContext` rather than relying on the passlib default.
- **AC-17 (State-driven):** While N concurrent `POST /token` requests are in flight, total wall
  time shall be materially less than N × single-hash time — the requests shall not serialize.
  Tested with N=8 (design §9.2).
- **AC-18 (Ubiquitous):** `app/db/seed.py`'s local `_hash_password` + `CryptContext` shall be
  deleted and delegated to `app.core.security.hash_password`, as its own docstring promises. The
  `bcrypt==4.0.1` pin (passlib 1.7.4 is incompatible with `bcrypt>=4.1`, per F12's DONE.md) shall
  remain.

### 3.4 Refresh & logout

- **AC-19 (Event-driven — rotation):** When a valid, unrevoked, unexpired refresh token is posted
  to `/api/auth/refresh`, the system shall, in **one transaction**: insert a new `refresh_tokens`
  row, set the old row's `revoked_at = now()` and its `replaced_by_jti` to the new jti, and return a
  new access + refresh pair.
- **AC-20 (Unwanted — reuse):** If a refresh token's jti has `revoked_at IS NOT NULL`, the system
  shall respond `401` and shall not issue tokens. (Detecting the reuse and revoking the whole
  rotation chain is a v2 concern — the chain is recorded in `replaced_by_jti` so it stays possible.)
- **AC-21 (Unwanted — wrong token type):** If a token with `typ != "refresh"` is posted to
  `/refresh`, the system shall respond `401`. An access token shall not be a valid refresh token.
- **AC-22 (Event-driven — logout):** When `POST /api/auth/logout` is called with a valid access
  token, the system shall set `revoked_at = now()` on the `refresh_tokens` row whose `jti` equals
  the access token's `sid`, and respond `204`.
- **AC-23 (Event-driven — logout kills access):** When a session has been logged out, a subsequent
  request bearing an **access** token from that session shall receive `401`, without waiting for the
  15-minute expiry (US-4).
- **AC-24 (Ubiquitous — idempotent):** Logging out an already-revoked session shall respond `204`
  and shall not overwrite the original `revoked_at`.

### 3.5 Principal resolution (dependencies)

- **AC-25 (Ubiquitous — validity rule):** A token shall be accepted only when its signature
  verifies, its `typ` matches the expected type, its `exp` has not passed (allowing
  `JWT_LEEWAY_S` = 30s of clock skew), a `refresh_tokens` row exists with `jti = sid` and
  `revoked_at IS NULL` and `expires_at > now()`, and the `users` row is `is_active`. This shall be
  resolved in a **single** awaited Postgres query (join), not one query per condition.
- **AC-26 (Ubiquitous — optional):** `get_current_user_optional` shall return `None` when no
  `Authorization` header and no `X-API-Key` header are present, and shall not query Postgres.
- **AC-27 (Unwanted — bad token on the optional path):** If a token *is* supplied but is invalid,
  `get_current_user_optional` shall respond `401` — it shall not silently downgrade to anonymous.
  (Absent means anonymous; wrong means wrong.)
- **AC-28 (Ubiquitous — generic 401s):** Every authentication failure shall use one generic detail
  string and shall not distinguish expired / malformed / revoked / unknown-user in the response body.
  The distinction shall appear in `structlog` only.
- **AC-29 (Ubiquitous — never log tokens):** No access token, refresh token, API key, password, or
  password hash shall appear in any log line, exception message, or `request_logs` row. A test shall
  assert this against a captured log stream for the full register→token→refresh→logout flow.
- **AC-30 (Event-driven — API key):** When an `X-API-Key: crag_<token>` header is present, the
  system shall look up `api_keys` by `key_hash = sha256hex(token)` with `revoked_at IS NULL`, join
  its owning user, and produce a principal whose kind is `api_key`.
- **AC-31 (Unwanted — revoked key):** If the matched `api_keys` row has a non-null `revoked_at`, or
  no row matches, the system shall respond `401`.
- **AC-32 (Ubiquitous — precedence):** When both `Authorization` and `X-API-Key` are present, the
  system shall use the JWT and ignore the API key — one documented precedence, no ambiguity.

### 3.6 Authorization matrix

The matrix the system shall enforce:

| Actor | `/api/ask` | rate tier | history | `/internal/*` |
|---|---|---|---|---|
| Anonymous | yes | 5/min per IP | no | no |
| Student (JWT) | yes | 20/min per user | own history | no |
| Admin (JWT) | yes | 60/min per user | own | yes (stats, cache flush, doc status, eval results) |
| API key (`X-API-Key`) | yes (ask-only scope) | 30/min per key | no | no |

- **AC-33 (Ubiquitous — admin gate):** `require_role("admin")` shall respond `403` for a student
  principal, `403` for an API-key principal, `401` for anonymous, and pass for an admin.
- **AC-34 (Ubiquitous — ask-only scope):** An API-key principal shall be rejected with `403` by
  `require_role` and by any endpoint other than `/api/ask` and `/api/auth/me`. The scope is a
  constant, not a DB column (§1.1).
- **AC-35 (Ubiquitous — rate tier resolver):** `rate_tier(principal)` shall be a pure function
  returning `(key, limit_per_min)` — `("ip:<ip>", 5)` anonymous, `("user:<id>", 20)` student,
  `("user:<id>", 60)` admin, `("apikey:<id>", 30)` API key — with every limit read from Settings.
  F11 shall consume it; F10 shall not implement Redis limiting.
- **AC-36 (Ubiquitous — matrix is a table test):** All sixteen cells of the matrix shall be covered
  by a parametrized test, not by prose.

### 3.7 `/api/auth/me`

- **AC-37 (Event-driven):** When called with a valid principal, `GET /api/auth/me` shall return
  `{id, email, role, is_active, created_at}` for JWT principals and the owning user for API-key
  principals.
- **AC-38 (Unwanted):** If no credentials are supplied, `/me` shall respond `401` (it is the one
  authed-only student endpoint).

### 3.8 Swagger

- **AC-39 (Ubiquitous):** The app shall use `OAuth2PasswordBearer(tokenUrl="api/auth/token")` so
  the Swagger Authorize button completes a real password flow, and shall declare an
  `APIKeyHeader(name="X-API-Key")` so the key path is documented too.
- **AC-40 (Event-driven):** After authorizing in Swagger, `GET /api/auth/me` shall return `200` from
  the Swagger UI with no manual header editing.

### 3.9 Retention & operations

- **AC-41 (Event-driven — prune):** When `python -m app.auth.run --prune` runs, the system shall
  delete `refresh_tokens` rows whose `expires_at < now()` and `login_attempts` rows whose
  `attempted_at < now() - LOGIN_LOCKOUT_WINDOW_MIN`, and print the counts. Deletion shall be
  driven by the existing `expires_at` / `attempted_at` indexes.
- **AC-42 (Ubiquitous — no in-app scheduler):** F10 shall ship the prune command and nothing that
  schedules it. The cron/Render job that calls it belongs to F15.
- **AC-43 (Event-driven — issue key):** When `python -m app.auth.run --issue-key --email <e>
  --label <l>` runs, the system shall generate `crag_<secrets.token_urlsafe(32)>`, store only its
  sha256 in `api_keys.key_hash`, and print the plaintext key **once**.
- **AC-44 (Event-driven — revoke key):** When `python -m app.auth.run --revoke-key --label <l>`
  runs, the system shall set `revoked_at = now()` on the matching row.

### 3.10 Async mandate & schema

- **AC-45 (Ubiquitous):** Every auth DB read/write shall use an awaited async SQLAlchemy session.
  The CI async-guard (no sync `Session`, `create_engine`, `requests`, sync `redis`) shall be
  extended to `app/auth/` and shall pass.
- **AC-46 (Ubiquitous — zero migrations):** F10 shall add no Alembic migration.
  `alembic revision --autogenerate` shall produce an **empty diff** after F10 lands — asserted by a
  test, so the "no new schema" claim cannot silently rot.
- **AC-47 (Ubiquitous — settings):** Every new config value shall live in the central `Settings`
  class (design §7). `JWT_SECRET` shall be required with **no default** — a defaulted signing
  secret is a shipped vulnerability.

## 4. Feature-card acceptance criteria (the definition of done)

Straight from the F10 card, each mapping to a test in `backend/tests/auth/test_acceptance.py`:

1. **Flow test:** register → token → authed ask-path principal → refresh → logout → old refresh
   rejected. (AC-1, 7, 19, 20, 22)
2. **Role tests:** student blocked from `/internal/*`; admin passes; API key limited to ask.
   (AC-33, 34, 36)
3. **Swagger Authorize flow works end-to-end.** (AC-39, 40)
4. **Bcrypt + blacklist + lockout unit-tested.** (AC-15, 17, 25, 13, 14)

## 5. Edge cases (decided, not open)

| Case | Decision |
|---|---|
| Access token expires mid-SSE stream | **Validate at request start only.** Killing a half-streamed answer to save ≤15 min of an already-authenticated stream is worse than the risk. Documented in the endpoint docstring and asserted by a test that expires a token mid-stream and expects the stream to complete. |
| Clock skew between API and client | `JWT_LEEWAY_S = 30`, passed to `jwt.decode(leeway=...)`. Applies to `exp` only. |
| Duplicate registration | `409` (AC-2), from a caught `IntegrityError` on the existing unique index — not a check-then-insert race. |
| Revoked API key | `401` (AC-31). Revocation is a DB write; there is no cache to invalidate. |
| `refresh_tokens` / `login_attempts` growth | `--prune` (AC-41). No TTL exists now that state is Postgres, which is the point — the tables are auditable. F12 already indexed `expires_at` and `attempted_at`. |
| Rotation vs. an in-flight access token | Rotating revokes the old family jti, so access tokens minted from it stop working (AC-23's mechanism). This is intended: clients refresh at ~15 min, which is when the access token was expiring anyway. Documented on `/refresh`. |
| Two devices, one account | Each login inserts its own `refresh_tokens` row and its own `sid` family. Logging out on one device does not touch the other. |
| Anonymous + `X-API-Key` garbage | `401` (AC-31), not anonymous fallback — consistent with AC-27. |
| `api_keys.key_hash` has no index | Deliberate. The table holds single-digit rows (bots, not users); a sequential scan is faster than the index. `ponytail:` comment names it with an upgrade path — add a unique index in a migration if keys ever number in the thousands. |

## 6. Out of scope

- **Rate limiting itself** — F11. F10 ships `rate_tier()` and nothing that touches Redis.
- **`/api/ask`, `/api/documents`, `/internal/stats|cache-flush|eval-results`** — F11/F13. F10 ships
  `/internal/ping` purely as the guarded mount point that makes AC-33 testable.
- **History endpoints** — F17 owns `sessions`/`messages`. F10 provides the `user_id` those rows
  hang off.
- **Writing `request_logs.user_id`** — F13. F10 exposes the principal; F13 logs it.
- **Password reset, email verification, social login** — CLAUDE.md v2 stretch, explicitly not now.
- **Refresh-reuse chain revocation** — v2. The `replaced_by_jti` chain is recorded so it stays a
  query, not a migration.
- **Admin UI / user management endpoints** — no consumer exists. F12's `seed_admin()` is the only
  admin provisioning path.
- **An eval gate** — §1.3.
