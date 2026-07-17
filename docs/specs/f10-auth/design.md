# F10 — Authentication & Authorization · design.md

**Module:** `backend/app/auth/` + `backend/app/core/security.py` · **Phase:** C ·
**Depends on:** F12 · **Flag:** none · **Migrations:** none · **Eval gate:** none

---

## 1. Module layout

```
backend/app/core/
├── security.py                 NEW  bcrypt + JWT + api-key hashing. Pure functions, no DB.
│                                    (CLAUDE.md repo structure: core/ = settings, security, …)
└── settings.py                 CHANGED  + "# --- Auth (F10) ---" block

backend/app/auth/               NEW package
├── __init__.py                 NEW  (empty)
├── schemas.py                  NEW  RegisterRequest, TokenResponse, UserOut, Principal
├── service.py                  NEW  the DB layer: register / authenticate / rotate / revoke /
│                                    resolve_jwt / resolve_api_key / prune / issue_key
├── deps.py                     NEW  FastAPI deps: get_current_user(_optional), require_role,
│                                    rate_tier  (the F11 seam)
└── run.py                      NEW  CLI: --prune | --issue-key | --revoke-key

backend/app/api/                NEW package  (first HTTP surface in the repo)
├── __init__.py                 NEW  (empty)
├── auth.py                     NEW  router: /api/auth/{register,token,refresh,logout,me}
└── internal.py                 NEW  router: /internal/ping  — admin-gated mount point for F13

backend/app/main.py             NEW  the FastAPI app. Mounts both routers. ~20 lines.

backend/app/db/seed.py          CHANGED  delete local _hash_password/CryptContext → core.security
backend/pyproject.toml          CHANGED  + pyjwt, python-multipart
backend/tests/auth/             NEW  conftest, test_security, test_service, test_api_auth,
                                     test_matrix, test_lockout, test_concurrency, test_logging,
                                     test_run, test_no_new_migration, test_settings, test_swagger,
                                     test_acceptance
.github/workflows/ci.yml        CHANGED  NEW `auth:` job (mirrors `caching:`)
.env.example                    CHANGED  + JWT_SECRET
```

**Nothing under `app/rag/`, `app/caching/`, `app/evals/`, or `app/db/models/` changes.** F10 is
additive to the HTTP edge; the pipeline does not know it exists. That is the whole point of the
`Principal`-or-`None` seam (§6).

## 2. The decision that removes most of the work: F12 already built the schema

| Table | Columns F10 needs | Present in `0001_initial`? |
|---|---|---|
| `users` | `id, email(unique), hashed_password, role(enum), is_active, created_at` | ✅ all |
| `refresh_tokens` | `id, user_id(FK cascade), jti(unique,indexed), issued_at, expires_at, revoked_at, replaced_by_jti, user_agent, ip` | ✅ all |
| `login_attempts` | `id, email_or_ip(indexed), attempted_at(indexed), success` + composite `(email_or_ip, attempted_at)` | ✅ all |
| `api_keys` | `id, user_id(FK cascade), key_hash, label, created_at, revoked_at` | ✅ all |

`app/db/models/auth.py`'s docstring already writes F10's two queries out longhand:

```
Validity rule (used by F10, not implemented here):  revoked_at IS NULL AND expires_at > now()
Lockout query (F10):  count(*) WHERE email_or_ip=:k AND success=false AND attempted_at > now()-15min
```

So: **no migration, no model edit** (AC-46). The single-scope API key (§5.3) and the unindexed
`key_hash` (§5.3) are the two decisions that keep it that way.

## 3. `app/core/security.py` — pure crypto, no DB

```python
_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=settings.BCRYPT_ROUNDS)

# A real bcrypt hash of a random string, computed at import. Verifying against it on an unknown
# email costs the same ~250ms as a real miss, so /token is not a timing oracle (AC-11).
_DUMMY_HASH: str = _pwd.hash(secrets.token_urlsafe(32))

async def hash_password(pw: str) -> str:                     # anyio.to_thread.run_sync  (AC-15)
async def verify_password(pw: str, hashed: str | None) -> bool:
    """`hashed=None` (unknown email) => verify against _DUMMY_HASH, return False. AC-11."""

def api_key_hash(raw: str) -> str:                           # sha256 hex; inline CPU (§5.3)
def new_api_key() -> tuple[str, str]:                        # ("crag_<token_urlsafe(32)>", hash)

def encode_access(user_id, role, sid) -> str                 # typ="access", exp=+15min, jti, sid
def encode_refresh(user_id, role) -> tuple[str, str, datetime]   # (token, jti, expires_at)
def decode_token(token: str, *, expect: Literal["access","refresh"]) -> dict:
    """jwt.decode(HS256, leeway=JWT_LEEWAY_S). Raises AuthError on bad sig / exp / typ mismatch.
    Pure CPU, ~microseconds — runs inline, NOT in a thread (see §8)."""
```

Which side of the CPU line each call falls on, per CLAUDE.md's mandate:

| Work | Side | Why |
|---|---|---|
| bcrypt `hash` / `verify` | **`anyio.to_thread.run_sync`** | ~250ms at cost 12, by design. Eight concurrent logins would be 2s of frozen event loop (AC-17). |
| `jwt.encode` / `jwt.decode` | **inline** | HMAC-SHA256 over ~200 bytes — microseconds. Same class as tiktoken counting, which CLAUDE.md explicitly allows inline. |
| `sha256` of an API key | **inline** | One 40-byte hash. |
| `secrets.token_urlsafe` | **inline** | CLI only. |

## 4. `app/auth/schemas.py` — `Principal` is the new contract

The Shared Context contracts (`AnswerResponse`, `Chunk`, …) are untouched. F10 adds one model, and
it is deliberately *not* a `User` ORM object — the pipeline should not hold a live ORM row across an
SSE stream that outlives its session.

```python
class Principal(BaseModel):
    kind: Literal["student", "admin", "api_key"]
    user_id: UUID          # API keys resolve to their owning user
    email: str
    role: UserRole         # api_key principals carry their owner's role but never get it (§5.4)
    api_key_id: UUID | None = None

    @property
    def is_admin(self) -> bool:  return self.kind == "admin"

class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)      # AC-3.  No `role` field — AC-6 by construction.

class TokenResponse(BaseModel):
    access_token: str; refresh_token: str; token_type: Literal["bearer"] = "bearer"

class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID; email: str; role: UserRole; is_active: bool; created_at: datetime
    # no hashed_password field => AC-1's "never respond with the hash" is structural
```

`AnswerResponse` gains **no** `user_id` field: `request_logs.user_id` (F13) and `sessions.user_id`
(F17) are written from the `Principal` at the router, not carried through the chain.

## 5. `app/auth/service.py` — the DB layer

Every function takes an `AsyncSession` explicitly (matching `app/rag/`'s style) and returns domain
objects or raises `AuthError`. No FastAPI imports here — testable without a client.

### 5.1 `authenticate(session, email, password, *, ip, user_agent) -> tuple[str, str]`

```
 1. locked = await _recent_failures(session, email) >= LOGIN_MAX_FAILURES   ── AC-13
    if locked: raise AuthError(429, "Too many failed attempts. Try again later.")
 2. user = await session.scalar(select(User).where(User.email == email))
 3. ok = await verify_password(password, user.hashed_password if user else None)  ── AC-11
    if not (ok and user and user.is_active):
        session.add(LoginAttempt(email_or_ip=email, success=False)); await session.commit()
        raise AuthError(401, GENERIC)                                    ── AC-10/11/12
 4. session.add(LoginAttempt(email_or_ip=email, success=True))
 5. refresh, jti, expires_at = encode_refresh(user.id, user.role)
    session.add(RefreshToken(user_id=user.id, jti=jti, expires_at=expires_at,
                             ip=ip, user_agent=user_agent))
 6. access = encode_access(user.id, user.role, sid=jti)                   ── AC-8
 7. await session.commit();  return access, refresh
```

Step 3 does the verify **before** the branch on `user is None`, not after. That ordering is the
whole timing-oracle defence — an early `if user is None: raise` would return in 2ms and leak the
answer regardless of what the message says.

The lockout key is the **email**, matching `login_attempts.email_or_ip`'s composite index. Keying on
IP as well would let one hostile IP lock out every account behind a shared NAT — a real risk in
Pakistani university labs and mobile carrier CGNAT. Column stays generic for F11's use.

### 5.2 `resolve_jwt(session, token) -> Principal` — AC-25, one query

```python
claims = decode_token(token, expect="access")          # sig + exp(leeway) + typ  (inline)
row = await session.execute(
    select(User)
    .join(RefreshToken, RefreshToken.user_id == User.id)
    .where(RefreshToken.jti == claims["sid"],          # the family this access token belongs to
           RefreshToken.revoked_at.is_(None),          # ← THE blacklist check (CLAUDE.md)
           RefreshToken.expires_at > func.now(),
           User.id == UUID(claims["sub"]),
           User.is_active.is_(True))
)
user = row.scalar_one_or_none()
if user is None: raise AuthError(401, GENERIC)         # AC-28: one message for all five reasons
return Principal(kind=user.role.value, user_id=user.id, email=user.email, role=user.role)
```

One indexed join (`refresh_tokens.jti` is unique-indexed, `users.id` is the PK) — not a query per
condition. **`role` comes from the `users` row, not from the JWT's `role` claim.** The claim exists
because the feature brief specifies it, but trusting it would mean a demoted admin keeps admin for
15 minutes; we are already loading the user, so the claim is decoration and the row is truth.

### 5.3 `resolve_api_key(session, raw) -> Principal` — AC-30

```python
user = await session.scalar(
    select(User).join(ApiKey, ApiKey.user_id == User.id)
    .where(ApiKey.key_hash == api_key_hash(raw),       # sha256, not bcrypt — §1.1 of requirements
           ApiKey.revoked_at.is_(None), User.is_active.is_(True))
)
if user is None: raise AuthError(401, GENERIC)
return Principal(kind="api_key", user_id=user.id, email=user.email, role=user.role,
                 api_key_id=...)
```

`kind="api_key"` — never `"admin"`, even when the key belongs to the admin user. The scope check
reads `kind`, so an admin's leaked bot key cannot reach `/internal/*` (AC-34). This is why `kind` is
a separate field from `role` instead of being derived from it.

`ponytail:` no index on `key_hash` — a handful of bot keys, so the seq scan wins. Add a unique index
(and a migration) if keys ever number in the thousands.

### 5.4 `rotate_refresh(session, token) -> tuple[str, str]` — AC-19, one transaction

```python
claims = decode_token(token, expect="refresh")                       # AC-21
old = await session.scalar(select(RefreshToken).where(
        RefreshToken.jti == claims["jti"], RefreshToken.revoked_at.is_(None),
        RefreshToken.expires_at > func.now()).with_for_update())     # AC-20
if old is None: raise AuthError(401, GENERIC)
user = await session.get(User, old.user_id)
if user is None or not user.is_active: raise AuthError(401, GENERIC)

new_refresh, new_jti, expires_at = encode_refresh(user.id, user.role)
old.revoked_at = func.now(); old.replaced_by_jti = new_jti           # the chain, for v2 reuse-detect
session.add(RefreshToken(user_id=user.id, jti=new_jti, expires_at=expires_at))
await session.commit()
return encode_access(user.id, user.role, sid=new_jti), new_refresh
```

`with_for_update()` is what makes two simultaneous refreshes of the same token produce one winner
and one `401` instead of two valid families. Without it the read-then-write is a classic race.

### 5.5 Others

```python
async def register(session, req) -> User            # IntegrityError -> AuthError(409)  (AC-2)
async def revoke_family(session, sid) -> None       # UPDATE ... WHERE jti=:sid AND revoked_at IS NULL
                                                    #   -> idempotent by the WHERE, not by a read (AC-24)
async def prune(session) -> tuple[int, int]         # AC-41
async def issue_key(session, email, label) -> str   # AC-43
async def revoke_key(session, label) -> int         # AC-44
```

## 6. `app/auth/deps.py` — the F11 seam

```python
_bearer  = OAuth2PasswordBearer(tokenUrl="api/auth/token", auto_error=False)   # AC-39
_api_key = APIKeyHeader(name="X-API-Key", auto_error=False)

async def get_current_user_optional(
    token: str | None = Depends(_bearer),
    key:   str | None = Depends(_api_key),
    session: AsyncSession = Depends(get_session),
) -> Principal | None:
    if token: return await resolve_jwt(session, token)     # JWT wins over key  (AC-32)
    if key:   return await resolve_api_key(session, key)
    return None                                            # anonymous; no DB hit (AC-26)

async def get_current_user(p = Depends(get_current_user_optional)) -> Principal:
    if p is None: raise AuthError(401, GENERIC)            # AC-38
    return p

def require_role(role: Literal["admin"]):                  # AC-33/34
    async def _dep(p: Principal = Depends(get_current_user)) -> Principal:
        if p.kind != role: raise AuthError(403, "Insufficient permissions")
        return p
    return _dep

def rate_tier(principal: Principal | None, ip: str) -> tuple[str, int]:   # AC-35 — PURE
    match principal:
        case None:                     return f"ip:{ip}",            s.RATE_LIMIT_ANON_PER_MIN
        case Principal(kind="api_key"):return f"apikey:{p.api_key_id}", s.RATE_LIMIT_API_KEY_PER_MIN
        case Principal(kind="admin"):  return f"user:{p.user_id}",   s.RATE_LIMIT_ADMIN_PER_MIN
        case _:                        return f"user:{p.user_id}",   s.RATE_LIMIT_STUDENT_PER_MIN
```

`get_current_user_optional` is the entire integration surface F11 needs for the ask path:

```python
# F11's /api/ask, for reference — NOT built here:
async def ask(req, principal: Principal | None = Depends(get_current_user_optional)):
    key, limit = rate_tier(principal, req.client.host)      # F11 hands this to its Redis limiter
    ...
    async for ev in baseline.astream(q, session=session, settings=settings): ...
```

`baseline.astream`'s signature is **unchanged** — no `principal` parameter. Auth is an edge concern;
the chain stays as testable-in-process as it is today, which is exactly what keeps the F4 harness
able to run with `session_id=None` and no principal at all.

## 7. Data flow

```
                    ┌───────────────── POST /api/auth/token ─────────────────┐
  form(username,pw) │  lockout window count ── login_attempts ──┐            │
        │           │              │ >=10 → 429                 │            │
        ▼           │              ▼                            ▼            │
   [ FastAPI ]──────┤   verify_password ──► anyio.to_thread ──► bcrypt(12)   │
                    │              │                                         │
                    │              ├─ fail → INSERT login_attempts(false) → 401 generic
                    │              └─ ok   → INSERT login_attempts(true)
                    │                        INSERT refresh_tokens(jti, expires_at)
                    │                        access = JWT{sub, role, jti, sid=jti, typ, exp+15m}
                    └────────────────► {access_token, refresh_token, bearer} ─┘

  Any authed request:
  Authorization: Bearer <access>
        │
        ▼
  decode_token(expect="access")          inline HMAC + exp(leeway 30s) + typ
        │
        ▼
  SELECT users JOIN refresh_tokens ON jti = claims.sid
    WHERE revoked_at IS NULL AND expires_at > now() AND users.is_active     ◄── ONE query (AC-25)
        │                                    │
        ▼                                    └── this IS the blacklist (CLAUDE.md)
   Principal(kind, user_id, email, role)
        │
        ├──► require_role("admin") ──► /internal/*        (403 for student & api_key)
        ├──► rate_tier() ──────────► F11's Redis limiter  (F10 stops at the tuple)
        └──► /api/ask (F11) ───────► baseline.astream(...)   ← principal not passed in

  POST /api/auth/refresh:  decode(expect="refresh") → SELECT ... FOR UPDATE → revoke old,
                           set replaced_by_jti, INSERT new  ── one transaction (AC-19)
  POST /api/auth/logout:   UPDATE refresh_tokens SET revoked_at=now()
                           WHERE jti = access.sid AND revoked_at IS NULL     → 204 (AC-22/24)
                           ⇒ every access token in that family dies now, not in 15 min (AC-23)
```

## 8. Error handling

One exception type, one place that maps it to HTTP:

```python
# app/core/exceptions.py (or auth/service.py if core/exceptions.py doesn't exist yet)
class AuthError(Exception):
    def __init__(self, status: int, detail: str): ...

GENERIC = "Could not validate credentials"      # AC-28: five failure modes, one string
```

Registered once in `main.py` as an exception handler → `JSONResponse(status, {"detail": ...})`,
adding `WWW-Authenticate: Bearer` on 401s. The *reason* is logged, never returned:

```python
log.info("auth.reject", reason="revoked_family", user_id=..., request_id=...)   # never the token
```

| Failure | Status | Body detail | Logged reason |
|---|---|---|---|
| Bad signature / malformed / expired / wrong `typ` | 401 | `GENERIC` | `bad_token` / `expired` / `wrong_typ` |
| Revoked or expired refresh family | 401 | `GENERIC` | `revoked_family` |
| Unknown email / wrong password / inactive | 401 | `Incorrect email or password` | `bad_credentials` |
| Locked out | 429 | `Too many failed attempts. Try again later.` | `lockout` |
| Student or API key at `/internal/*` | 403 | `Insufficient permissions` | `forbidden` |
| Duplicate registration | 409 | `Email already registered` | `duplicate_email` |
| Password < 8 / bad email | 422 | Pydantic's own | — |
| Postgres down during auth | 500 | `GENERIC` | `auth_backend_error` |

**Auth never fails open.** This is the deliberate inverse of F9's "the cache is an optimization,
never a failure source" — a cache error degrades to a slower answer; an auth error must degrade to
*no* answer. Note the asymmetry only bites `/api/auth/*` and `/internal/*`: if Postgres is down, the
anonymous ask path never touches `get_current_user_optional`'s DB branch and keeps working.

**AC-29 (never log tokens)** is enforced structurally: no log call in `app/auth/` takes a token,
key, password, or hash as a value, and `test_logging.py` runs the full flow against a captured
structlog stream asserting none of the four secrets appears in any event.

## 9. New Settings keys

Appended to the one `Settings` class as a `# --- Auth (F10) ---` block:

```python
# --- Auth (F10) ---
JWT_SECRET: SecretStr                       # REQUIRED, no default — AC-47
JWT_ALGORITHM: str = "HS256"                # fixed stack (pyjwt HS256)
JWT_LEEWAY_S: int = 30                      # clock skew allowance on exp
ACCESS_TOKEN_TTL_MIN: int = 15
REFRESH_TOKEN_TTL_DAYS: int = 7
BCRYPT_ROUNDS: int = 12                     # explicit; not passlib's default-by-accident (AC-16)
AUTH_EMAIL_DOMAIN_ALLOWLIST: list[str] = [] # e.g. ["edu.pk"]; empty => off (AC-5, default off)
LOGIN_MAX_FAILURES: int = 10                # AC-13
LOGIN_LOCKOUT_WINDOW_MIN: int = 15
# Rate tiers — the §3.6 matrix as config. F10 resolves the tier; F11 enforces it.
RATE_LIMIT_ANON_PER_MIN: int = 5
RATE_LIMIT_STUDENT_PER_MIN: int = 20
RATE_LIMIT_ADMIN_PER_MIN: int = 60
RATE_LIMIT_API_KEY_PER_MIN: int = 30
```

Reused, **not** redefined: `DATABASE_URL`, `ADMIN_EMAIL`, `ADMIN_PASSWORD` (F12).
`REDIS_URL` stays F9's — F10 does not read it.

`JWT_SECRET` being required means every conftest env stub, `.env.example`, and the CI job needs it
(tasks T1). That friction is the feature: a default signing key is a vulnerability that ships.

## 10. New dependencies

```toml
# --- Auth (F10) ---
"pyjwt==2.10.1",           # fixed stack. NOT python-jose (unmaintained, CVE history).
"python-multipart==0.0.20", # REQUIRED by OAuth2PasswordRequestForm — FastAPI parses the
                            # password grant as multipart/form-data. Without it /token raises
                            # at import with a runtime error, not a clean ImportError. Easy to miss.
```

`passlib[bcrypt]==1.7.4` + `bcrypt==4.0.1` are **already pinned** by F12 — reused, not re-pinned.
That `bcrypt==4.0.1` pin is load-bearing: passlib 1.7.4 reads `bcrypt.__about__.__version__`, which
`bcrypt>=4.1` removed, producing a spurious *"password cannot be longer than 72 bytes"* on every
hash. F12's DONE.md flags this explicitly for F10. Do not bump it without replacing passlib.

## 11. Migrations

**None** (AC-46). §2 is the argument. `tests/auth/test_no_new_migration.py` runs
`alembic revision --autogenerate` after `upgrade head` and asserts an empty diff — the same guard
F9's T2 used, inverted: F9 proved its one migration matched the models; F10 proves it needs none.

## 12. Honoring the Shared Context contracts & the F3 retriever seam

| Contract | How F10 honors it |
|---|---|
| `AnswerResponse` | **Unchanged.** No `user_id` field added — F13 writes `request_logs.user_id` from the `Principal` at the router. |
| SSE event contract (`stage → token → citations → meta → done\|error`) | **Unchanged.** Auth resolves before the first `stage` event; a 401 is an HTTP status, not an `error` event, because the stream never opens. |
| F3 LCEL retriever seam | **Untouched.** `retriever.retrieve()` and `baseline.astream()` gain no parameters. Auth never reaches the chain. |
| `PipelineFlags` | **Unchanged.** Auth is not a pipeline flag (requirements §1.2). |
| Postgres table ownership | F10 writes `users`, `refresh_tokens`, `login_attempts`, `api_keys` — all F12-owned, referenced by name, schema untouched. |
| Async/await mandate | Every DB call awaited on an async session; bcrypt via `anyio.to_thread.run_sync`; JWT/sha256 inline (§3's table). CI's async-guard extends to `app/auth/`. |
| "Every metric mentioned must be logged" | F10 mentions no pipeline metric. It emits `auth.register`, `auth.login`, `auth.reject`, `auth.rotate`, `auth.logout`, `auth.prune` via structlog — reasons only, never secrets (AC-29). |
| `estimate_cost()` | Not applicable — F10 makes zero OpenAI calls. |
| F4 harness comparability | The harness calls `baseline.answer()` in-process with no HTTP layer. It never constructs a `Principal`. Retrieval metrics are structurally unaffected — the basis for requirements §1.3. |

## 13. Test strategy

`backend/tests/auth/conftest.py` mirrors `tests/cache/conftest.py`: own `engine`/`session`
fixtures, the `get_engine`/`get_sessionmaker` `lru_cache` reset (F12's DONE.md documents why
pytest-asyncio's per-test event loop demands it), autouse env stubs **including `JWT_SECRET`**, and
`users`/`refresh_tokens`/`login_attempts`/`api_keys` added to the autouse `TRUNCATE` teardown.

> ⚠️ **The corpus-wipe trap.** `tests/rag` truncates `documents`/`chunks` in the shared DB, which is
> how past eval runs got `false_refusal_rate=1.0`. F10's teardown must truncate **only its four
> tables** — never `documents`/`chunks` — and must not share a DB with an in-flight eval run.

| File | Covers |
|---|---|
| `test_settings.py` | Defaults exactly as §9; missing `JWT_SECRET` → `ValidationError` (AC-47) |
| `test_security.py` | Round-trip encode/decode; tampered sig → raise; expired → raise; 29s-expired token passes on leeway, 31s fails (AC-25); `typ` mismatch raises (AC-21); api-key hash stability |
| `test_service.py` | register/409, authenticate happy + all three 401s, rotate, revoke idempotency, prune counts, issue/revoke key |
| `test_lockout.py` | 10 failures → 429 without a bcrypt call; window ages out → success (AC-13/14) |
| `test_concurrency.py` | 8 concurrent `/token` complete in `< 8 × single_hash × 0.6` (AC-17) |
| `test_matrix.py` | Parametrized over all 16 cells of §3.6 (AC-36) |
| `test_api_auth.py` | Endpoint-level: status codes, no hash in any response body, `WWW-Authenticate` header |
| `test_logging.py` | Full flow vs. captured structlog: no token/key/password/hash substring (AC-29) |
| `test_swagger.py` | `/openapi.json` declares `OAuth2PasswordBearer` at `api/auth/token` + `APIKeyHeader` (AC-39) |
| `test_no_new_migration.py` | Empty autogenerate diff (AC-46) |
| `test_acceptance.py` | The four feature-card criteria (requirements §4) |

`.github/workflows/ci.yml` gains an `auth:` job mirroring `caching:`: Postgres service →
`alembic upgrade head` → `pytest tests/auth` → ruff + the async-guard grep extended to `app/auth/`,
plus the new grep for direct `CryptContext` use outside `core/security.py` (AC-15).
