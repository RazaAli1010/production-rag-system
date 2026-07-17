# F11 — API Hardening · requirements.md

**Module:** `backend/app/api/` (+ `app/core/` middleware, `app/api/health.py`) · **Phase:** C
(production layer) · **Depends on:** F3 (pipeline + SSE + `degraded`/`ProviderError`), F5–F9
(flags + `AnswerResponse` fields), F10 (`rate_tier`, principals, `AuthError`), F17 (`/api/ask`,
sessions router, per-session lock) · **Flag:** `ENABLE_RATE_LIMIT` (default `true`) · **New model:**
none · **Blocks:** F14 (frontend builds from this OpenAPI), F16 (Telegram)

---

## 1. Overview

F11 turns the routers F17/F10 already shipped into the **final public HTTP surface**: validated
inputs, per-tier Redis-backed rate limiting, a single error envelope, request-id correlation,
gzip/CORS/timing middleware, a server-side timeout, client-disconnect cancellation, per-dependency
health, and a complete OpenAPI contract F14 can build against with zero backend spelunking.

F11 is **not** a retrieval feature. It changes no chunking, no scoring, no prompt. It wraps the
existing `astream`/`answer` pipeline (F3, unchanged) and the existing `/api/ask` + `/api/sessions`
routers (F17, unchanged in behaviour). Its "quality gate" is therefore a **latency/overhead check**
(the hardening layer must not regress p50/p95), not a new hit@k label — the eval-label sequence
still ends at `f17-memory-after`.

### 1.1 Design decisions resolved in the feature brief (do NOT re-derive)

- **The routes already exist.** `api/ask.py` (SSE, memory, per-session lock, write-behind,
  disconnect-safe assistant write) and `api/sessions.py` (CRUD, 404-on-foreign) shipped in F17;
  `api/auth.py` and `api/internal.py` shipped in F10. F11 **wraps** these — it does not rewrite the
  pipeline or the memory path. The one behavioural change to `/api/ask` is the **input contract**
  (question `3–500` chars, optional `namespace`/`deep`/`skip_cache`/`flags_override`) and
  content-negotiation (SSE default, JSON on `Accept: application/json`).
- **Tier resolution already exists.** F10's `auth.deps.rate_tier(principal, ip)` already returns
  `(bucket_key, limit_per_min)` for anon/student/admin/api_key. F11 **consumes** it — it does not
  re-derive tiers. The `RATE_LIMIT_*_PER_MIN` values are already in `Settings` (F10).
- **Redis storage is mandatory, not a library choice.** The rate-limit counter MUST live in Redis so
  the limit holds across uvicorn workers and Render replicas (design §4 states why an in-memory
  limiter is wrong). The project already depends on `redis==5.2.1` (`redis.asyncio`; the sync client
  is banned) and F9 already runs it. F11 adds **no rate-limit dependency** — a fixed-window limiter is
  ~20 lines of `INCR`/`EXPIRE` over the existing async client (design §4). `slowapi`/`fastapi-limiter`
  are rejected as unnecessary deps whose default IP-only keying we'd have to override anyway (we key
  by principal via `rate_tier`).
- **The error taxonomy already exists.** F3 ships `rag.errors.ProviderError` (LLM/embeddings hard
  fail → **503**) and F5 ships `AnswerResponse.degraded` (Pinecone down → BM25-only answer). F10 ships
  `core.exceptions.AuthError` (**401/403**) with its handler already in `main.py`. F11 adds the
  **envelope** (`{error:{type,message,request_id}}`) and the handlers that render each typed error
  into it — it does not invent new failure classes for cases already modelled.
- **`request_id`/`latency_ms` are the fields F11 finally populates.** The canonical `AnswerResponse`
  reserves both; the `core.contracts` comment says "F13 owns request identity" only because no
  consumer existed yet. F11 is that consumer: the request-id middleware stamps a contextvar, the ask
  route measures wall time, and both land on the `meta` SSE event / JSON body (additive fields,
  defaults, **no migration** — `AnswerResponse` is not a table).
- **`request_logs` rows are written by F13, not F11.** Build order is F11 → F13, and every feature's
  `observability.py` already defers the `request_logs` write to F13. So `GET /api/history` reads
  `request_logs` and is **correct but empty until F13 populates it** (AC-16) — this is an ordering
  fact, not a bug. F11 does not add a second write path.
- **No new table, no migration (AC-24).** `documents`, `request_logs`, `sessions`, `messages` were all
  migrated by F12; `AnswerResponse` is a wire model. `alembic revision --autogenerate` stays empty.

## 2. User stories

**US-1 (Frontend dev):** As the F14 dev, I want a complete OpenAPI doc with schemas and examples and
working auth from `/docs`, so I can build the chat UI from the contract without reading backend code.

**US-2 (Student on mobile):** As a student, I want a garbage or 2000-char question rejected with a
clear `422`, so I get an instant, readable error instead of a wasted 4-second pipeline run.

**US-3 (Ops / cost owner):** As the person paying the bill, I want each tier rate-limited by a counter
shared across all replicas, so one client can't fan out over N workers and get N× its quota.

**US-4 (Student):** As a rate-limited caller, I want a `429` with a `Retry-After` header, so my client
knows exactly when to try again instead of hammering.

**US-5 (Ops):** As an operator, I want `GET /api/health` to tell me per-dependency status (Pinecone,
Postgres, Redis, BM25, OpenAI key) so I can tell a Pinecone outage from a Postgres outage at a glance.

**US-6 (Student):** As a student whose Pinecone is down, I want a best-effort BM25 answer marked
`degraded=true` rather than a hard 5xx, so the assistant still helps when the dense index is out.

**US-7 (Student on a flaky connection):** As a mobile user who backgrounds the app mid-answer, I want
the server to stop generating (and stop billing) the moment I disconnect, so a dropped connection
never runs a full LLM call into the void.

**US-8 (Ops):** As an operator, I want a request stuck past 30s to end in a clean SSE `error` event,
so a wedged upstream never holds a connection open forever.

**US-9 (Any client):** As any caller, I want every error to look the same —
`{error:{type,message,request_id}}` — and every response to carry the request id, so I can quote one
id in a bug report and ops can grep it.

**US-10 (Returning user):** As a logged-in user, I want `GET /api/history` to list my recent requests
and `GET /api/documents` to list the corpus, so the UI can show "what I asked" and "what's covered".

**US-11 (Ops):** As an operator, I want rate limiting switchable off without a deploy, so a
misbehaving limiter is one flag away from open.

## 3. EARS acceptance criteria

### 3.1 Endpoints & validation

- **AC-1 (Ubiquitous — ask contract):** `POST /api/ask` shall accept
  `{question: str (3–500 chars), session_id?: uuid, namespace?: "pu"|"hec", deep?: bool,
  skip_cache?: bool, flags_override?: dict}`; a `question` outside `3–500` chars, an unknown
  `namespace`, or a non-admin caller supplying `flags_override` shall be rejected `422` (`flags_override`
  → `403`) before any pipeline work runs.
- **AC-2 (Event-driven — content negotiation):** When `/api/ask` is called with
  `Accept: application/json`, the system shall return one JSON `AnswerResponse`; otherwise it shall
  return the SSE stream (`stage*→token*→citations→meta→done|error`) unchanged from F17.
- **AC-3 (Event-driven — deep mode):** When `deep: true` is sent by an authorized caller, the pipeline
  shall run with `gpt-4o` (F3's deep-mode model) instead of `gpt-4o-mini`; otherwise the primary model
  is used. (F11 only plumbs the flag to the existing F3 seam; it does not change generation logic.)
- **AC-4 (Event-driven — skip_cache):** When `skip_cache: true` is sent, the F9 cache lookup/write
  shall be bypassed for that request (mapping the request flag F9 already supports to an HTTP field).
- **AC-5 (Event-driven — history):** When an authenticated caller requests `GET /api/history`, the
  system shall return that user's last `HISTORY_PAGE_SIZE=50` `request_logs` rows newest-first
  (populated once F13 writes them; empty before that, AC-16).
- **AC-6 (Event-driven — documents):** When `GET /api/documents` is called, the system shall return the
  corpus listing (`doc_id, title, source_org, version_label, file_type, url, status`) from the
  `documents` table.
- **AC-7 (State-driven — health):** `GET /api/health` shall report per-dependency status for Pinecone,
  Postgres, Redis, BM25-loaded, and OpenAI-key-present, checked concurrently
  (`asyncio.gather`, each guarded by a short timeout); it shall return `200` when all core deps are up
  and `503` when any core dep is down, with the failing dependency named.

### 3.2 Rate limiting

- **AC-8 (State-driven — per-tier limit):** While `ENABLE_RATE_LIMIT` is true, each request to a
  rate-limited route shall be counted against the Redis bucket returned by `rate_tier(principal, ip)`,
  and a request exceeding that tier's per-minute limit shall be rejected `429`.
- **AC-9 (Event-driven — Retry-After):** When a `429` is returned, the response shall carry a
  `Retry-After` header (seconds until the current window rolls over).
- **AC-10 (Ubiquitous — shared counter):** The rate-limit counter shall live in Redis so the limit is
  enforced across all uvicorn workers and API replicas; an in-memory limiter is explicitly disallowed
  (design §4).
- **AC-11 (Unwanted — Redis down):** If Redis is unreachable while `ENABLE_RATE_LIMIT` is true, the
  limiter shall **fail open** (allow the request) and log `ratelimit.redis_unavailable` — a limiter
  outage shall never take the whole API down. (Fail-open is the deliberate choice for a student-facing
  read API; a fail-closed variant is a v2 note.)
- **AC-12 (State-driven — toggle):** While `ENABLE_RATE_LIMIT` is false, no bucket is read or written
  and no `429` is ever returned.

### 3.3 Errors, middleware, timeouts

- **AC-13 (Ubiquitous — error envelope):** Every error response from the API shall have body
  `{"error": {"type": <slug>, "message": <safe text>, "request_id": <id>}}` and the matching HTTP
  status; `ProviderError`→`503`, `AuthError`→`401/403` (unchanged from F10), validation→`422`,
  rate limit→`429`, unhandled→`500` with a generic message (never a stack trace or DB detail).
- **AC-14 (Ubiquitous — request id):** Every request shall be assigned a `request_id` (generated, or
  taken from an inbound `X-Request-ID`), stored in a `contextvar` so every log line and the error
  envelope carry it, and echoed in the `X-Request-ID` response header and on `AnswerResponse.request_id`.
- **AC-15 (Ubiquitous — middleware):** The app shall apply gzip (responses ≥ `GZIP_MIN_BYTES`), an
  `X-Response-Time` header (wall-clock ms), and CORS restricted to `CORS_ALLOW_ORIGINS` from Settings
  — never a wildcard when credentials are allowed.
- **AC-16 (Ubiquitous — degraded, not down):** If Pinecone fails during retrieval, the request shall
  return a best-effort BM25-only answer with `degraded=true` (the F5 behaviour) rather than `5xx`;
  `/api/health` shall independently report `pinecone=down`.
- **AC-17 (Event-driven — server timeout):** When a request exceeds `REQUEST_TIMEOUT_S=30`, the system
  shall terminate the pipeline and emit a terminal SSE `error` event (JSON variant → `504`), never an
  indefinitely open stream.
- **AC-18 (Unwanted — client disconnect cancels work):** If the client disconnects mid-stream, the
  server shall cancel the downstream LLM/pipeline coroutine (no further token generation, no assistant
  write-behind) — verified by an async cancellation test.

### 3.4 OpenAPI, async mandate, settings & gate

- **AC-19 (Ubiquitous — OpenAPI):** `GET /openapi.json` and `/docs` shall document every endpoint with
  request/response schemas and at least one example each, and `/docs` shall let a user paste a bearer
  token and call authed endpoints; F14 shall be buildable from this contract alone.
- **AC-20 (Ubiquitous — async surface):** All F11 code shall be async: health checks via async clients
  (`SELECT 1`, `redis.asyncio.ping`, Pinecone stats in a thread if the client is sync), the rate
  limiter via `redis.asyncio`; no blocking `requests`, no sync redis, no `create_engine` in
  `app/api|app/core` (CI-guarded).
- **AC-21 (Ubiquitous — Settings centralisation):** Every new config value (`ENABLE_RATE_LIMIT`,
  `CORS_ALLOW_ORIGINS`, `REQUEST_TIMEOUT_S`, `GZIP_MIN_BYTES`, `HISTORY_PAGE_SIZE`) shall live in the
  single `Settings` class; no module shall read `os.environ`. Existing `RATE_LIMIT_*_PER_MIN` and
  `EVAL_LATENCY_ENDPOINT` are reused, not redefined.
- **AC-22 (State-driven — toggle parity):** While `ENABLE_RATE_LIMIT` is false and no
  `namespace`/`deep`/`flags_override` is sent, `/api/ask` behaviour shall be identical to the
  F17-shipped route (same SSE bytes, same memory path) — proved by a regression test.
- **AC-23 (Ubiquitous — every metric logged):** `X-Response-Time`, the request id, rate-limit
  decisions (`ratelimit.allow`/`ratelimit.reject`/`ratelimit.redis_unavailable`), and timeout/disconnect
  events shall each be logged via `structlog`; F13 later routes them into `request_logs`/Langfuse
  without an F11 change.
- **AC-24 (Ubiquitous — no migration):** F11 shall add NO Alembic revision; `alembic revision
  --autogenerate` shall stay empty (design §7).
- **AC-25 (Ubiquitous — overhead gate):** F11 shall not be done until
  `docs/eval_results/f11-api-hardening-overhead.md` is committed, running the F4 latency suite against
  the live `/api/ask` endpoint (`EVAL_LATENCY_ENDPOINT` set) and comparing p50/p95/cost to
  `f17-memory-after`'s in-process numbers, confirming the middleware + limiter overhead is bounded
  (target: added p50 well under the network/LLM budget; no cost change since retrieval is unchanged).

## 4. Acceptance criteria (feature-level definition of done)

1. **SSE + JSON:** `curl -N /api/ask` streams for both authed and anonymous callers; the same call
   with `Accept: application/json` returns an `AnswerResponse` whose fields match the stream's `meta`
   plus the assembled answer (AC-1/2).
2. **Validation:** a 2-char and a 600-char question each get `422` with the envelope; a non-admin
   `flags_override` gets `403` — no pipeline runs (AC-1/13).
3. **Rate limit:** a tier exceeding its `RATE_LIMIT_*_PER_MIN` gets `429` + `Retry-After`; the count is
   shared across two worker processes (AC-8/9/10); `ENABLE_RATE_LIMIT=false` never 429s (AC-12);
   Redis down fails open (AC-11).
4. **Health:** `/api/health` returns per-dependency status; with the Pinecone key removed it reports
   `pinecone=down` and returns `503`, yet `/api/ask` still answers `degraded=true` (AC-7/16).
5. **Disconnect:** an async test that cancels the client mid-stream verifies the pipeline coroutine is
   cancelled and no assistant message is persisted (AC-18).
6. **Timeout:** a stubbed slow pipeline past `REQUEST_TIMEOUT_S` yields a terminal SSE `error` event /
   `504` (AC-17).
7. **Envelope + request id:** every error path returns `{error:{type,message,request_id}}` and every
   response carries `X-Request-ID`; an inbound `X-Request-ID` is echoed (AC-13/14).
8. **OpenAPI:** `/docs` renders all endpoints with examples and authorizes with a bearer token (AC-19).
9. **Toggle parity:** `ENABLE_RATE_LIMIT=false` + no new flags is byte-identical to F17 `/api/ask`
   (AC-22).
10. **No-migration check:** `alembic revision --autogenerate` is empty (AC-24).
11. **Overhead gate:** `docs/eval_results/f11-api-hardening-overhead.md` committed (AC-25).

## 5. Out of scope (do not implement here)

- **`request_logs` row writing + Langfuse spans** — F13. F11 emits structlog + populates the
  `meta`/envelope fields F13 will route; `GET /api/history` reads the table F13 fills.
- **Second-provider LLM fallback** — documented as v2 in the brief; `ProviderError`→`503` is the F11
  behaviour.
- **Locust load tests / deploy** — F15. F11's gate is the in-process F4 latency suite against the live
  endpoint, not a distributed load test.
- **New retrieval/generation behaviour** — F11 changes no chunking, scoring, prompt, or refusal logic;
  it wraps the F3–F9/F17 pipeline unchanged.
- **`/internal/stats`** — F13 owns the admin stats endpoint (F10 already mounted the admin-guarded
  `/internal` router).
- **New DB tables / any schema change** — F12 already migrated everything F11 reads (AC-24).
</content>
</invoke>
