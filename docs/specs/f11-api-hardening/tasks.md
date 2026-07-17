# F11 — API Hardening · tasks.md

**Module:** `backend/app/api/` + `backend/app/core/` · **Phase:** C · **Depends on:** F3–F10, F17 ·
**Flag:** `ENABLE_RATE_LIMIT` · **Overhead gate:** F4 latency suite vs `f17-memory-after`

Each task is ≈ ≤ 1 hour and lands green. Order is boundary-inward: Settings + contract fields first,
then the leaf pieces that touch nothing else (middleware, limiter, error envelope, health/documents/
history read routes), then the one behavioural change to `/api/ask` (validation, JSON variant,
timeout), then wiring in `main.py`, then toggle parity, then the overhead gate. The F3/F17 pipeline is
never edited — F11 wraps it.

**No Alembic task exists on purpose** — F12 already migrated every table F11 reads (design §10). T13
*asserts* autogenerate stays empty; adding a migration is a design regression.

**T14 IS the feature.** Per CLAUDE.md, a Phase C feature is not done when the code works; it is done
when `docs/eval_results/f11-api-hardening-overhead.md` is committed.

---

### T1 — Settings block + `AnswerResponse` fields + test scaffold
Add the `# --- API hardening (F11) ---` block from design §9 to `app/core/settings.py`
(`ENABLE_RATE_LIMIT`, `CORS_ALLOW_ORIGINS`, `REQUEST_TIMEOUT_S`, `GZIP_MIN_BYTES`, `HISTORY_PAGE_SIZE`,
`RATE_LIMIT_WINDOW_S`). Add `request_id: str | None = None` and `latency_ms: int | None = None` to
`core.contracts.AnswerResponse` (additive, design §5). Create `backend/tests/api/conftest.py` mirroring
`tests/auth/conftest.py` (own engine/session, autouse env stubs, an `httpx.AsyncClient`/ASGI transport
fixture, a fake `redis.asyncio` fixture).

**Test:** `tests/api/test_settings_schemas.py` — defaults exactly `ENABLE_RATE_LIMIT is True`,
`REQUEST_TIMEOUT_S == 30.0`, `HISTORY_PAGE_SIZE == 50`, `CORS_ALLOW_ORIGINS == []`; `AnswerResponse()`
defaults `request_id is None`, `latency_ms is None` (AC-21, contract additive).

---

### T2 — `app/core/middleware.py` (request_id + X-Response-Time)
`ContextVar[str] request_id_var`; an HTTP middleware that reads inbound `X-Request-ID` or generates
`uuid4().hex`, sets the var, binds `structlog.contextvars`, times the request, and sets `X-Request-ID`
+ `X-Response-Time` response headers (design §8). No CORS/gzip here — those are stdlib middleware wired
in `main.py` (T11).

**Test:** `tests/api/test_middleware.py` (against a throwaway app) — a response carries both headers;
an inbound `X-Request-ID` is echoed verbatim; two concurrent requests get distinct ids (contextvar
isolation, AC-14).

---

### T3 — `app/core/errors.py` (envelope + handlers)
`envelope(type_, message)` reading `request_id_var` (design §6). Handlers for `RequestValidationError`
(422), `RateLimited` (429 + `Retry-After`), `ProviderError` (503), `asyncio.TimeoutError` (504),
and a catch-all `Exception` (500, generic message — never a traceback/DB detail). `RateLimited`
exception class defined here. The F10 `AuthError` handler in `main.py` is left as-is.

**Test:** `tests/api/test_errors_envelope.py` — each handler yields
`{"error":{"type","message","request_id"}}` with the right status; the 500 handler never leaks the
raised message; `RateLimited` sets `Retry-After` (AC-13).

---

### T4 — `app/core/ratelimit.py` (Redis fixed-window limiter)
`check(bucket, limit, *, redis, window_s)` per design §4: `INCR` + first-hit `EXPIRE`, over-limit →
`RateLimited(retry_after=ttl)`, **fail open** + `ratelimit.redis_unavailable` on any Redis error. A
FastAPI dependency `rate_limit_dep(request, principal)` that resolves ip + `rate_tier()` and calls
`check` when `ENABLE_RATE_LIMIT`; a no-op when the flag is off.

**Test:** `tests/api/test_ratelimit.py` (fake async redis) — the (limit+1)th call in a window raises
`RateLimited` with `retry_after>0`; a fresh window resets; `ENABLE_RATE_LIMIT=false` never raises
(AC-8/9/12); a redis that raises on `incr` lets the call through and logs `ratelimit.redis_unavailable`
(fail-open, AC-11). One test asserts the window-id-in-key so two "workers" sharing the fake store see
one shared count (AC-10).

---

### T5 — `app/api/health.py`
`GET /api/health` per design §7: concurrent `_pg`/`_redis`/`_pinecone`/`_bm25`/`_openai_key`, each in
a 2s `wait_for`; Pinecone stats via `anyio.to_thread.run_sync` (sync client off the loop); Redis
`skipped` when `REDIS_URL is None`; OpenAI key is presence-only (no live call). `200` when core deps
up, `503` naming the down dep.

**Test:** `tests/api/test_health.py` — all-up → `200` with per-dep `ok`; a stubbed Pinecone failure →
`503` with `pinecone: down`; a stubbed 5s-hanging probe is cut at 2s and reported down, not hung
(AC-7). No live OpenAI call is made (assert the client is never invoked).

---

### T6 — `app/api/documents.py` + `app/api/history.py`
`GET /api/documents` → corpus listing (`doc_id, title, source_org, version_label, file_type, url,
status`) from `documents` (AC-6). `GET /api/history` (authed) → last `HISTORY_PAGE_SIZE` `request_logs`
rows for `principal.user_id`, newest-first (AC-5) — **note in the module docstring** that rows appear
once F13 writes them (build-order fact, not a bug).

**Test:** `tests/api/test_history_documents.py` — seed two `documents` rows → listing returns them;
`/api/history` unauthenticated → 401; authed with an empty `request_logs` → `[]` (correct pre-F13);
authed with two seeded rows → newest-first, capped at the page size.

---

### T7 — `/api/ask` input contract + validation
Tighten `AskRequest` (design §5): `question` `3–500`, `namespace: Literal["pu","hec"] | None`,
`deep`/`skip_cache: bool`, `flags_override: dict[str,bool] | None`. Validate `flags_override` keys
against `PipelineFlags` field names and require `principal.kind == "admin"` (else 403). Map `namespace`/
`deep`/`skip_cache` onto the existing `astream`/`answer` params (deep → `gpt-4o`). Memory path, per-
session lock, write-behind UNCHANGED.

**Test:** `tests/api/test_ask_contract.py` — 2-char and 600-char questions → 422 envelope, no pipeline
call (stub `astream`, assert not awaited); unknown `namespace` → 422; non-admin `flags_override` → 403;
admin `flags_override` passes through; `deep=true` selects the deep model at the seam (AC-1/3/4).

---

### T8 — `/api/ask` JSON variant + request_id/latency stamping
When `Accept: application/json`, collect the stream via `rag.baseline.answer()` and return one
`AnswerResponse`; else the SSE `StreamingResponse` (unchanged). Stamp `request_id` (contextvar) +
`latency_ms` (route timer) onto the `meta` event and the JSON body (design §5). Do not thread these
through `baseline.py`.

**Test:** `tests/api/test_ask_json.py` (stubbed pipeline) — the JSON body is a valid `AnswerResponse`
whose non-answer fields equal the SSE `meta`, plus the reassembled answer text; both carry
`request_id` matching `X-Request-ID`; SSE remains the default without the Accept header (AC-2/14).

---

### T9 — Server timeout + client-disconnect cancellation
Wrap the pipeline in `async with asyncio.timeout(settings.REQUEST_TIMEOUT_S)`; on timeout emit the
terminal SSE `error` event (JSON variant → 504) and log `api.timeout`. Rely on Starlette generator
cancellation for disconnect (F17's clean-`done` gate already blocks the assistant write); add an
`api.client_disconnect` log in the generator's cancellation path.

**Test:** `tests/api/test_timeout_disconnect.py` — a stubbed pipeline that sleeps past the timeout
yields a terminal `error` event / 504 (AC-17); a test that cancels the consuming task mid-stream
asserts the pipeline coroutine received `CancelledError` and `schedule_persist_assistant` was never
called (AC-18).

---

### T10 — Rate-limit dependency on write routes
Attach `rate_limit_dep` to `POST /api/ask` and `POST /api/sessions`. Confirm `rate_tier()` keys
anon by ip, student/admin by user, api_key by key id (reused from F10, not reimplemented).

**Test:** `tests/api/test_ratelimit_routes.py` (real fake-redis, real routes, stubbed pipeline) —
`RATE_LIMIT_ANON_PER_MIN+1` anonymous asks → the last is 429 with `Retry-After`; an admin token gets
the higher admin ceiling; `ENABLE_RATE_LIMIT=false` never 429s (AC-8/9/12).

---

### T11 — Wire `main.py` (middleware, handlers, routers, CORS/gzip)
Register `request_id` middleware, `GZipMiddleware(minimum_size=GZIP_MIN_BYTES)`,
`CORSMiddleware(allow_origins=CORS_ALLOW_ORIGINS, allow_credentials=True)`, the T3 exception handlers,
and the `health`/`documents`/`history` routers. Keep the F10 `AuthError` handler.

**Test:** `tests/api/test_app_wiring.py` — a response carries `Content-Encoding: gzip` for a body over
the threshold and the `X-Response-Time`/`X-Request-ID` headers; a disallowed `Origin` gets no CORS
allow header, an allowed one does; an unhandled error returns the 500 envelope, not a traceback
(AC-13/15).

---

### T12 — OpenAPI completeness + async/secrets CI guard
Add `summary`/`description` + at least one example to each endpoint's route decorator and response
model so `/openapi.json` is F14-buildable; confirm `/docs` authorizes with a bearer token. Add the
`api:` CI job (design §12) mirroring `auth:` — Postgres + Redis services, `pytest tests/api`, async-
guard grep over `app/api app/core` (ban sync redis, `import requests`, `create_engine`, `.invoke(`),
`ruff check`.

**Test:** `tests/api/test_openapi.py` — `/openapi.json` contains every F11 path with a request/response
schema and ≥1 example; the `securitySchemes` include the bearer flow; the async-guard grep finds no
banned symbol (AC-19/20).

---

### T13 — Toggle parity + no-migration assertion
Regression test that `ENABLE_RATE_LIMIT=false` with no `namespace`/`deep`/`flags_override` produces the
same SSE byte stream and memory behaviour as the F17-shipped `/api/ask` (AC-22). Assert
`alembic revision --autogenerate` yields an empty diff (AC-24).

**Test:** `tests/api/test_toggle_parity.py` — captured SSE frames equal the F17 baseline for an
identical seeded session; `tests/api/test_no_migration.py` runs autogenerate and asserts the script
body is empty.

---

### T14 — Overhead gate (definition of done)
Run the F4 latency suite against the **live** endpoint (`EVAL_LATENCY_ENDPOINT` set to the running
`/api/ask` URL, already supported by F4) and `--compare` the `f17-memory-after` in-process latency
label. Write `docs/eval_results/f11-api-hardening-overhead.md`: p50/p95/cost before (in-process) vs
after (through the full middleware + limiter + validation stack), confirming added p50 is bounded and
cost is unchanged (retrieval/generation untouched). Map the run to a git SHA + index manifest.

**DoD — all AC met:**
1. SSE + JSON both work, authed and anonymous (AC-1/2).
2. Validation rejects out-of-bounds question / bad namespace / non-admin override with the envelope
   before any pipeline work (AC-1/13).
3. Per-tier 429 + `Retry-After`, shared across workers via Redis, fail-open on Redis down, off when
   flagged off (AC-8/9/10/11/12).
4. `/api/health` per-dependency status; Pinecone key removed → `pinecone=down`/503 yet `/api/ask`
   still answers `degraded=true` (AC-7/16).
5. Client disconnect cancels the pipeline and persists no assistant message (AC-18); 30s timeout →
   terminal `error`/504 (AC-17).
6. Uniform `{error:{type,message,request_id}}` envelope + `X-Request-ID` on every response (AC-13/14).
7. `/docs` builds F14 with working bearer auth (AC-19).
8. Toggle parity with F17 proven; autogenerate empty (AC-22/24).
9. `docs/eval_results/f11-api-hardening-overhead.md` committed (AC-25).
</content>
