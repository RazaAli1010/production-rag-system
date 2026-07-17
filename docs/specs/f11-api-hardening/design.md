# F11 — API Hardening · design.md

**Module:** `backend/app/api/` + `backend/app/core/` · **Phase:** C · **Depends on:** F3–F10, F17 ·
**Flag:** `ENABLE_RATE_LIMIT` · **New model:** none · **Overhead gate:** F4 latency suite vs
`f17-memory-after`

---

## 1. Module layout

```
backend/app/core/
├── middleware.py                    NEW  request_id contextvar + X-Response-Time; CORS/gzip wired
│                                         in main.py from stdlib starlette middleware (no new dep)
├── ratelimit.py                     NEW  async Redis fixed-window limiter (INCR/EXPIRE) + FastAPI
│                                         dependency; fails open on Redis error (AC-8/9/10/11)
├── errors.py                        NEW  the {error:{type,message,request_id}} envelope builder +
│                                         the exception handlers (ProviderError→503, validation→422,
│                                         429, 500). AuthError handler already lives in main.py (F10)
├── settings.py                      CHANGED  + "API hardening (F11)" block (§7)
└── exceptions.py                    UNCHANGED  AuthError reused

backend/app/api/
├── ask.py                           CHANGED  input contract (3–500 chars, namespace/deep/skip_cache/
│                                             flags_override), Accept: json variant, 30s timeout,
│                                             request_id+latency_ms onto meta/response. Memory path,
│                                             per-session lock, write-behind, disconnect gate UNCHANGED
├── health.py                        NEW  GET /api/health — per-dependency concurrent probe
├── history.py                       NEW  GET /api/history — authed request_logs read (empty pre-F13)
├── documents.py                     NEW  GET /api/documents — corpus listing
├── sessions.py                      CHANGED  rate-limit dependency added to POST; else UNCHANGED (F17)
├── auth.py / internal.py            UNCHANGED  (F10)
└── deps.py (auth)                   UNCHANGED  rate_tier() reused verbatim

backend/app/core/contracts.py        CHANGED  AnswerResponse += request_id/latency_ms (additive, §5)
backend/app/main.py                  CHANGED  middleware + exception handlers + new routers registered

backend/tests/api/                   NEW  test_ask_contract, test_ask_json, test_ratelimit,
                                          test_health, test_history_documents, test_errors_envelope,
                                          test_timeout_disconnect, test_toggle_parity, test_openapi,
                                          test_no_sync_calls
.github/workflows/ci.yml             CHANGED  NEW `api:` job (mirrors `auth:`) — §8
```

Nothing under `app/rag/`, `app/memory/`, `app/indexing/` changes: F11 is the wrapper, the pipeline
is a black box it calls (`astream`/`answer`).

## 2. Key design decision: wrap, don't rebuild

The single most important property of F11 is that it **adds a shell and touches no core**. The routes
(`ask.py`, `sessions.py`) and the pipeline (`baseline.astream/answer`) already do the hard work; F11's
job is the boundary concerns FastAPI/Starlette and one small limiter give us almost for free:

| Concern | Reused / native (chosen) | Rejected (why) |
|---|---|---|
| gzip | `starlette.middleware.gzip.GZipMiddleware` | a custom compressor — starlette ships one |
| CORS | `starlette.middleware.cors.CORSMiddleware` | hand-rolled headers |
| tier keys + limits | F10 `auth.deps.rate_tier()` | re-deriving tiers in F11 |
| rate-limit storage | existing `redis.asyncio` client (F9) + `INCR`/`EXPIRE` | `slowapi`/`fastapi-limiter` deps whose default IP-only keying we'd override anyway |
| LLM-hard-fail → 503 | F3 `rag.errors.ProviderError` | a new error class |
| Pinecone-down → degraded | F5 `AnswerResponse.degraded` | a new 5xx path |
| disconnect cancellation | F17 already gates the assistant write on clean `done`; Starlette cancels the generator on disconnect | a manual poll loop |
| validation + OpenAPI | Pydantic `Field` bounds on the request model | manual checks + hand-written docs |

`ponytail:` the limiter is ~20 lines over the client we already run. If tiering ever needs sliding
windows or burst tokens, swap in `limits`/`fastapi-limiter` then — not before the fixed window is
observed to be too coarse.

## 3. Data flow (pipeline order — F11 seams in **bold**)

```
POST /api/ask
   │  [middleware] assign request_id (contextvar) ─ inbound X-Request-ID honored (AC-14)
   │  [middleware] start X-Response-Time timer, gzip, CORS (AC-15)
   │
   ├─ **validate**  AskRequest: question 3–500, namespace∈{pu,hec}?, deep?, skip_cache?,   (AC-1)
   │                flags_override (admin-only → 403 else) — 422 with envelope on failure
   ├─ **auth**      get_current_user_optional (F10) — principal | None
   ├─ **rate limit** ratelimit.check(rate_tier(principal, ip)) → 429 + Retry-After           (AC-8/9)
   │                 (skipped when ENABLE_RATE_LIMIT=false; fail-open on Redis error, AC-11/12)
   │
   ├─ **timeout**   async with asyncio.timeout(REQUEST_TIMEOUT_S):                            (AC-17)
   │      └─ F17 memory + F3 pipeline, UNCHANGED:
   │         load memory → summarize? → rewrite(F7) → cache(F9, skip_cache honored) →
   │         hybrid(F5) → rerank(F6) → refusal → compress(F8) → generate(F3) → cache write →
   │         persist assistant (F17 write-behind) → SSE: stage*→token*→citations→meta→done|error
   │
   ├─ Accept: application/json ─► collect via answer() → one AnswerResponse (AC-2)
   │  else                     ─► StreamingResponse (SSE), meta stamped w/ request_id+latency_ms
   │
   └─ on client disconnect: Starlette cancels the generator → pipeline coroutine cancelled,     (AC-18)
      no further tokens, no assistant write (F17's clean-`done` gate already guarantees this)
```

## 4. Rate limiter — `core/ratelimit.py`

Fixed-window counter keyed by the F10 tier bucket. One `INCR`; on the first hit of a window set the
TTL. Over limit → `429` with `Retry-After = ttl`.

```python
async def check(bucket: str, limit: int, *, redis, window_s: int = 60) -> None:
    """Raise RateLimited(retry_after) if this request exceeds `limit` in the current window.
    Redis-backed so the count is shared across workers/replicas (AC-10). Fails OPEN on any Redis
    error (AC-11) — a limiter outage must not take the API down."""
    key = f"campusrag:rl:{bucket}:{int(time.time() // window_s)}"   # window id in the key = auto-expire
    try:
        n = await redis.incr(key)
        if n == 1:
            await redis.expire(key, window_s)
    except Exception as exc:                       # redis.asyncio errors only
        logger.warning("ratelimit.redis_unavailable", error=str(exc)); return   # fail open
    if n > limit:
        ttl = await redis.ttl(key)
        raise RateLimited(retry_after=max(ttl, 1))
```

Exposed as a FastAPI dependency `rate_limit_dep` that resolves principal + ip, calls `rate_tier`, and
`check(...)` — attached to `POST /api/ask` and `POST /api/sessions`. `RateLimited` is handled by the
envelope handler (§6) which sets the `Retry-After` header.

**Why in-memory is wrong (AC-10, brief requirement):** an in-process counter lives per uvicorn worker
and per Render replica. With `w` workers a client gets `w×limit`, and the number silently drifts as we
scale. Redis is the one shared counter every replica increments, so the limit is the limit regardless
of topology. The window id lives *in the key* (`…:<epoch//60>`), so old windows expire by TTL with no
sweep job.

`ponytail:` fixed window allows a 2× burst across a boundary (limit at 0:59, limit again at 1:00).
Acceptable for a student read API; upgrade to a sliding window only if abuse is observed.

## 5. Request/response contract & `AnswerResponse` changes

`api/ask.py` request model tightened (validation + OpenAPI both come from `Field`):

```python
class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=500)           # AC-1; frozen wire contract, F14 depends
    session_id: uuid.UUID | None = None
    namespace: Literal["pu", "hec"] | None = None
    deep: bool = False                                            # AC-3 → F3 deep-mode model
    skip_cache: bool = False                                      # AC-4 → F9 bypass
    flags_override: dict[str, bool] | None = None                 # admin-only (AC-1), else 403
```

`question`'s `3–500` bound is the public API contract F14 builds from, so it lives in the schema (like
the `{doc_id}:{seq}` id format), not in `Settings`. `flags_override` is validated against
`PipelineFlags` field names and requires `principal.kind == "admin"`.

`core.contracts.AnswerResponse` gains the two reserved fields (additive, defaults, **no migration** —
it is a wire model, mirrors how F9 added `tokens_in/out` and F5 added `degraded`):

```python
class AnswerResponse(BaseModel):
    ...
    request_id: str | None = None   # NEW (F11) — correlation id, from the request contextvar
    latency_ms: int | None = None   # NEW (F11) — server wall-clock for the request
```

The ask route stamps both onto the `meta` event / JSON body from the contextvar + timer — **not**
threaded through `baseline.py` (the pipeline stays request-identity-agnostic; F13 later reads the same
contextvar for `request_logs`). This keeps the F17 pipeline seam untouched (AC-22 parity).

## 6. Error handling & envelope — `core/errors.py`

One builder + typed handlers registered in `main.py`:

```python
def envelope(type_: str, message: str) -> dict:
    return {"error": {"type": type_, "message": message, "request_id": request_id_var.get()}}
```

| Exception (source) | Status | `type` slug | Message policy |
|---|---|---|---|
| `RequestValidationError` (FastAPI) | 422 | `validation_error` | field errors, safe |
| `RateLimited` (F11) | 429 | `rate_limited` | + `Retry-After` header |
| `ProviderError` (F3) | 503 | `provider_unavailable` | "upstream provider unavailable" |
| `asyncio.TimeoutError`/`TimeoutError` (F11) | 504 / SSE `error` | `timeout` | "request timed out" |
| anything else | 500 | `internal_error` | generic — never a stack trace or DB detail (AC-13) |

The envelope covers the **typed** error classes above. Two categories deliberately keep FastAPI's
`{"detail": ...}` shape instead:

- **`AuthError` (F10)** — its existing handler already renders a generic non-oracle body; wrapping it
  would change nothing and risk the F10 tests. AC-13 explicitly carves it out ("unchanged from F10").
- **Raw `HTTPException`s the routes raise** — `404 not found`, `409 session_busy` (F17), `403
  flags_override requires admin` (F11). Starlette matches `HTTPException`'s own handler before the
  catch-all `Exception`, so these return `{"detail": ...}`. F17's suite asserts
  `{"detail": "session_busy"}`, so this shape is load-bearing and preserved.

Pinecone failure is **not** in this table: F5 already downgrades it to a `degraded=true` answer inside
the pipeline, so it never reaches a handler (AC-16). `/api/health` reports it independently (§ below).

Inside the SSE stream a mid-stream failure/timeout is emitted as the contract's terminal `error`
event (not an HTTP status — the `200` stream already started); the JSON variant maps to the status
above. The existing `main.py` `AuthError` handler is kept; F11 adds the rest.

## 7. Health probe — `api/health.py`

```python
async def health(...) -> JSONResponse:
    checks = await asyncio.gather(
        _pg(session), _redis(), _pinecone(), _bm25(), _openai_key(),  # each wrapped in wait_for(2s)
        return_exceptions=True,
    )
    # core deps = pg, redis(if configured), pinecone, bm25; openai_key is presence-only.
    status = 200 if all_core_up else 503
```

- `_pg`: `await session.execute(text("SELECT 1"))`.
- `_redis`: `await redis.ping()` — `skipped` when `REDIS_URL` is None (F9 makes Redis optional).
- `_pinecone`: `describe_index_stats` — the Pinecone client is sync, so run it via
  `anyio.to_thread.run_sync` (AC-20, CPU/blocking-off-loop rule); a missing/invalid key → `down`.
- `_bm25`: `settings.BM25_PATH.exists()` (cheap `stat`, inline).
- `_openai_key`: presence of `OPENAI_API_KEY` — **no** live call (never bill a health check).

Each probe has its own 2s `asyncio.wait_for`; one slow dep can't hang the endpoint (AC-7).

## 8. Middleware — `core/middleware.py` + `main.py`

- **request_id:** a `ContextVar[str]`; middleware reads inbound `X-Request-ID` or generates
  `uuid4().hex`, sets the var, binds it into `structlog.contextvars` so every log line carries it,
  and sets the `X-Request-ID` response header (AC-14).
- **X-Response-Time:** `perf_counter` delta in ms as a response header (AC-15).
- **gzip:** `GZipMiddleware(minimum_size=settings.GZIP_MIN_BYTES)` — Starlette streams it; SSE frames
  are small and below the threshold, so streaming is unaffected.
- **CORS:** `CORSMiddleware(allow_origins=settings.CORS_ALLOW_ORIGINS, allow_credentials=True, ...)`
  — allowlist only; an empty list means no cross-origin (dev uses the Vite proxy). Never `*` with
  credentials (AC-15).

## 9. New Settings keys (`# --- API hardening (F11) ---`)

```python
ENABLE_RATE_LIMIT: bool = True           # prod toggle; False ≡ F17 route, no 429 (AC-12/22)
CORS_ALLOW_ORIGINS: list[str] = []       # exact-origin allowlist; empty ⇒ no cross-origin (AC-15)
REQUEST_TIMEOUT_S: float = 30.0          # server-side ask timeout → SSE error / 504 (AC-17)
GZIP_MIN_BYTES: int = 500                # gzip threshold (AC-15)
HISTORY_PAGE_SIZE: int = 50              # GET /api/history page (AC-5)
RATE_LIMIT_WINDOW_S: int = 60            # fixed-window size for the limiter (AC-8)
```

Reused, **not** redefined: `RATE_LIMIT_ANON/STUDENT/ADMIN/API_KEY_PER_MIN` (F10), `REDIS_URL` +
`CACHE_REDIS_TIMEOUT_S` (F9), `EVAL_LATENCY_ENDPOINT` (F4, the live-endpoint gate hook, §10), `LLM_MODEL`
+ the deep-mode `gpt-4o` (F3). All config in the one `Settings` class; nothing reads `os.environ`
(AC-21).

## 10. Alembic migration

**None.** `documents`, `request_logs`, `sessions`, `messages` were migrated by F12; `AnswerResponse`
is a wire model, not a table. `AskRequest`/health/history/documents responses are Pydantic, not ORM.
AC-24 asserts `alembic revision --autogenerate` yields an empty diff after F11.

## 11. Honoring Shared Context contracts & the F3 seam

- **SSE contract** is preserved exactly — F11 adds no event type and no stage; it stamps `request_id`
  + `latency_ms` onto the existing `meta` payload and (for the JSON variant) collects the same stream
  via F3's `answer()`. F14 needs no contract change beyond the two new `meta` fields (AC-2/19).
- **The F3 retriever seam is untouched.** `namespace`/`deep`/`skip_cache` map onto parameters
  `astream`/`answer` already accept (`namespace`, model selection, F9 `skip_cache`); F11 passes them
  through, changing no retrieval or generation code (AC-3/4, parity AC-22).
- **`AnswerResponse`** gains only the two reserved identity fields the canonical contract always
  listed — additive with defaults, same discipline as F5/F9 (§5).
- **F10 auth** is consumed as-is: `get_current_user_optional` for `/api/ask`, `rate_tier` for limits,
  `require_role("admin")` for `flags_override` and the existing `/internal` router.
- **F17 memory + F9 cache** compose unchanged: F11 only tightens the input and wraps the stream; the
  per-session lock, write-behind, and cache key (F7 standalone question) are all inside the wrapped
  pipeline.

## 12. CI (`api:` job, mirrors `auth:`)

Real Postgres service; run `tests/api`; async-guard over `app/api` + the three new `app/core` files
(ban `import requests`, sync `redis`, `create_engine(`) plus a scoped `ruff check` (F11-owned files
only — `app/core/settings.py`/`contracts.py` carry pre-existing long-comment E501s that no job lints,
and F11 does not adopt that debt); plus a no-migration guard (`python -m alembic revision
--autogenerate` must emit no schema op, AC-24). Postgres is real because health `SELECT 1`,
`/api/history` and `/api/documents` hit it. **Redis and the pipeline are faked** — the limiter is
exercised against a shared `FakeRedis` (which proves AC-10's cross-worker property without a server)
and `astream` is stubbed, so the job makes zero OpenAI/Pinecone/Redis network calls and needs only
placeholder keys (the `auth:` env block, reused).

## 13. Metrics logged (every metric named is logged)

`ratelimit.allow` / `ratelimit.reject` (bucket, limit) / `ratelimit.redis_unavailable`;
`api.timeout` (request_id, elapsed_ms); `api.client_disconnect` (request_id); the `X-Response-Time`
value; the request id on every line via `structlog.contextvars`. All via `structlog` — F13 routes
them into `request_logs`/Langfuse without an F11 change (the pre-existing per-feature convention).
</content>
