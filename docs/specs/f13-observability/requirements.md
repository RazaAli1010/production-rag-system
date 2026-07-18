# F13 — Observability · requirements.md

**Module:** `backend/app/observability/` (+ one line in `main.py` lifespan, one endpoint on
`app/api/internal.py`, one write-behind call in `app/api/ask.py`) · **Phase:** C (production layer) ·
**Depends on:** F11 (request-id contextvar, error envelope, `/api/history` reader, `/internal`
router, structlog call sites), F12 (`request_logs`, `cache_entries`, `sessions`, `messages` tables),
F3/F5–F9/F17 (the per-stage `log_*` structlog emitters + `langfuse_handler` + `estimate_cost` all
already exist) · **Flag:** none new — Langfuse is toggled by presence of `LANGFUSE_*` keys (F3
already made it None-safe); `request_logs` writing is always on · **New model:** none ·
**Blocks:** F14 (stats dashboard, "what I asked" history), F15 (prod dashboards)

---

## 1. Overview

F13 makes every `/api/ask` request **traceable across three sinks that already have their plumbing
half-run by earlier features**:

1. **Postgres `request_logs`** — the source of truth. F12 migrated the table with every column F13
   needs; F11's `/api/history` already reads it; every enhancement's `log_*` emitter already computes
   the numbers. **The one thing missing is the writer.** F13 adds a single write-behind row per ask.
2. **Langfuse traces** — spans + per-span tokens. F3 already ships a None-safe `langfuse_handler`
   attached to the F3 chain. F13 extends it with `trace_id = request_id` and an env tag, nothing more.
3. **structlog JSON logs** — F11 already binds `request_id` into a contextvar and every feature
   already emits `logger.info("rag.*", …)`. **The one thing missing is a central `configure()`** so
   those lines render as JSON with the request-id merged in. F13 adds it in the boot lifespan.

Plus the admin **`GET /internal/stats`** endpoint (the `/internal` router already exists, admin-guarded).

### 1.1 Design decisions resolved up front (do NOT re-derive)

- **No new table, no migration.** `request_logs` (`app/db/models/ops.py`) already has
  `request_id, ts, user_id, session_id, channel, query_hash, pipeline_flags, cache_hit, refused,
  degraded, memory_summarized, embed_ms, retrieve_ms, rerank_ms, rewrite_ms, memory_ms, summarize_ms,
  llm_ms, total_ms, tokens_in, tokens_out, est_cost_usd, model, http_status, error_type`. That is
  exactly F13's row. `alembic revision --autogenerate` stays empty (AC-13).
- **The metrics are already computed — F13 only collects and persists them.** Each feature's
  `app/rag/observability.py::log_*` (rerank/rewrite/compression/cache/llm_cost) and F17's stage
  emitter (`app/memory/stages.py`) already produce the per-stage timings and token/cost numbers as
  structlog events. F13 adds a request-scoped accumulator those seams also write into, and flushes it
  once. It does **not** re-instrument the pipeline or re-time anything.
- **The cost model already exists and is already central.** `app/indexing/cost.py::estimate_cost`
  with its per-1M-token `_RATES` table is imported by F2/F3/F6–F9/F17. F13 reuses it verbatim as the
  single `estimate_cost()` the brief mandates. It is **not** duplicated into `Settings` (see §5,
  Deviation note) — relocating a working central helper would touch six files for zero behaviour change.
- **`request_id` identity is already built.** F11's `RequestContextMiddleware` sets
  `core.middleware.request_id_var` (honoring inbound `X-Request-ID`) and the error envelope already
  carries it. F13 reads that same contextvar for the Langfuse trace id and the log correlation — it
  invents no new id scheme.
- **Write path is write-behind, off the response path.** The `request_logs` insert runs in an
  `asyncio.create_task` fired after the response is assembled (same pattern F17 already uses for the
  assistant-message write). It never adds to the ask's p50/p95 (AC-11).
- **Privacy is structural, not a filter.** `request_logs` has **no** `query_text` column — only
  `query_hash` (reusing `app/caching/keys.py::exact_key`). Raw query text therefore *cannot* land in
  telemetry. Chat `messages` (F17) store raw text by design — that is user-visible product data, a
  different table with a different purpose. The privacy test asserts the hash, never the text (AC-14).
- **`request_logs` rows are written for `/api/ask` only.** Health checks, auth, doc listings, and
  stats reads get structlog lines but no `request_logs` row — those are not pipeline requests and
  logging them would pollute every rate/latency aggregate. History and stats are about asks.

## 2. User stories

**US-1 (On-call ops):** As the person paged at 3am, I want to take one `request_id` from a bug report
and find its Langfuse trace, its Postgres `request_logs` row, and its correlated JSON log lines, so I
can reconstruct exactly what one request did without guessing.

**US-2 (Cost owner):** As the person paying the OpenAI bill, I want every request's token usage and
`est_cost_usd` persisted through one cost model, so the monthly spend is auditable per request, per
model, per flag — not estimated after the fact.

**US-3 (Ops / product):** As an operator, I want `GET /internal/stats?window=24h` to show request
count, p50/p95 latency, cache hit rate, refusal rate, error rate, degraded rate, total cost, tokens
saved by cache, per-flag usage, top query clusters, active sessions, mean turns/session, and
summarization count, so I can see system health and the ROI of each enhancement at a glance.

**US-4 (Returning user, via F11):** As a logged-in student, I want `GET /api/history` to actually
return rows, so the UI can show "what I asked" — which needs F13 to start writing `request_logs`.

**US-5 (Free-tier dev):** As a developer with no Langfuse account, I want the app to boot and run with
Langfuse absent (no callback, no error), so observability is never a hard dependency.

**US-6 (Privacy / compliance):** As the person answerable for student data, I want a test proving raw
query text never reaches `request_logs`, so telemetry stays hash-only by construction.

**US-7 (Debugging):** As a developer, I want every OpenAI / Pinecone / Redis call to log its duration
and outcome as structured JSON carrying the request id, so I can grep one id and see the whole call
chain in order.

## 3. EARS acceptance criteria

### 3.1 Langfuse

- **AC-1 (Event-driven — trace per request):** When Langfuse keys are configured, each `/api/ask`
  chain run shall attach the callback handler with `trace_id = request_id` (from
  `core.middleware.request_id_var`) and a `tags=[APP_ENV]` env tag, producing spans for
  rewrite → retrieve → rerank → generate with per-span token usage. (F3's existing handler; F13
  extends its signature to also read the request id + env.)
- **AC-2 (Unwanted — graceful no-op):** If `LANGFUSE_PUBLIC_KEY` or `LANGFUSE_SECRET_KEY` is absent,
  `langfuse_handler` shall return `None` and the pipeline shall run with no callback attached and no
  error (the F3-shipped behaviour, preserved).

### 3.2 Postgres request_logs

- **AC-3 (Event-driven — one row per ask):** When an `/api/ask` request completes (success, refusal,
  degraded, or handled error), the system shall write exactly one `request_logs` row per the F12
  schema, populated from the request-scoped metrics accumulator + the final `AnswerResponse`:
  stage timings (`embed_ms, retrieve_ms, rerank_ms, rewrite_ms, memory_ms, summarize_ms, llm_ms,
  total_ms`), `tokens_in/out`, `est_cost_usd`, `model`, `pipeline_flags`, `cache_hit`, `refused`,
  `degraded`, `memory_summarized`, `channel`, nullable `user_id`/`session_id`, `http_status`, and
  `error_type` (null on success).
- **AC-4 (Ubiquitous — write-behind):** The `request_logs` insert shall run in an
  `asyncio.create_task` off the response path (mirroring F17's assistant-message write); it shall
  never block the SSE stream or the JSON response, and a write failure shall be logged, not raised
  to the client.
- **AC-5 (Ubiquitous — hash only):** The row's `query_hash` shall be `exact_key(normalized_query)`
  (the same F9 helper `cache_entries.query_hash` uses, so the two join for query-cluster stats); no
  raw query text shall be written to `request_logs`.
- **AC-6 (State-driven — ask-only):** A `request_logs` row shall be written for `/api/ask` requests
  only; health, auth, documents, history, and stats requests shall produce structlog lines but no
  `request_logs` row.

### 3.3 structlog

- **AC-7 (Ubiquitous — central config):** At app startup the system shall call one
  `configure_logging(settings)` that installs JSON rendering (console renderer when `LOG_JSON=false`),
  a `merge_contextvars` processor (so the F11 `request_id` and `APP_ENV` appear on every line), an ISO
  timestamp, and `LOG_LEVEL` filtering. No module shall call `structlog.configure` itself.
- **AC-8 (Ubiquitous — per-call duration + outcome):** Every OpenAI, Pinecone, and Redis call site
  shall log its duration and outcome as structured JSON (the existing `log_*`/`log_llm_cost` emitters
  satisfy this for the pipeline; F13 verifies the coverage, it does not re-add call sites).
- **AC-9 (Ubiquitous — correlation):** Every log line emitted while handling a request shall carry the
  request's `request_id`, so one id greps the whole call chain (AC-1's trace, AC-3's row, these lines
  all share it).

### 3.4 Stats endpoint

- **AC-10 (Event-driven — admin stats):** When an admin calls `GET /internal/stats?window=<Nh|Nd>`
  (default `STATS_DEFAULT_WINDOW_H=24`), the system shall return, aggregated over that window:
  request count; p50/p95 `total_ms`; cache hit rate; refusal rate; error rate (`http_status >= 500`);
  degraded rate; total `est_cost_usd`; tokens saved by cache; per-flag usage counts (from
  `pipeline_flags`); top query clusters (by `cache_entries.hits`, joined on `query_hash`); active
  sessions; mean turns per session; and summarization count (rows where `memory_summarized`). Each
  aggregate is a SQL query over `request_logs` / `cache_entries` / `sessions` / `messages`; values not
  directly stored (tokens saved by summarization) are derived and marked approximate.

### 3.5 Cost, settings, async, gate

- **AC-11 (Ubiquitous — one cost model):** Every persisted `est_cost_usd` shall come from the single
  `app/indexing/cost.py::estimate_cost`; F13 shall not introduce a second price table.
- **AC-12 (Ubiquitous — Settings + async):** New config values (`APP_ENV`, `LOG_LEVEL`, `LOG_JSON`,
  `STATS_DEFAULT_WINDOW_H`) shall live in the single `Settings` class; the `request_logs` write and
  all stats queries shall use the async SQLAlchemy session (asyncpg), no sync DB access in
  `app/observability`/`app/api` (CI-guarded).
- **AC-13 (Ubiquitous — no migration):** F13 shall add NO Alembic revision;
  `alembic revision --autogenerate` shall stay empty.
- **AC-14 (Ubiquitous — privacy test):** A test shall assert that after an `/api/ask` run, the
  `request_logs` row contains the query hash and that the raw query string appears in no
  `request_logs` column.
- **AC-15 (Ubiquitous — observability gate):** F13 shall not be done until the three acceptance
  criteria below are proven; because F13 changes no retrieval or generation, it adds **no** new
  eval-gate hit@k label (the sequence still ends at `f17-memory-after`, as F11 established for the
  production layer). Its gate is functional correlation + a write-behind overhead sanity check, not a
  new delta report.

## 4. Acceptance criteria (feature-level definition of done)

1. **One id → three sinks (AC-1/3/9):** an `/api/ask` run with Langfuse configured yields a Langfuse
   trace, a `request_logs` row, and JSON log lines that all carry the same `request_id`; a test
   asserts the row exists and the id matches the response's `X-Request-ID`.
2. **Stats aggregates (AC-10):** `GET /internal/stats` returns every field in AC-10, unit-tested
   against seeded `request_logs` / `cache_entries` / `sessions` / `messages` fixtures with known
   expected values; non-admin gets 403 (router guard).
3. **Privacy (AC-14):** the privacy test passes — hash present, raw text absent from `request_logs`.
4. **Graceful Langfuse (AC-2):** with `LANGFUSE_*` unset, the same ask runs green and still writes its
   `request_logs` row + logs (Langfuse just no-ops).
5. **No migration (AC-13):** `alembic revision --autogenerate` is empty.
6. **Write-behind overhead (AC-4/15):** a test/timing note shows the `request_logs` write runs in a
   fired task and the ask response returns without awaiting it (added p50 ≈ 0).

## 5. Out of scope (do not implement here)

- **New retrieval/generation behaviour, new eval label** — F13 observes; it changes no chunking,
  scoring, prompt, refusal, or memory logic, so there is no new hit@k label (AC-15).
- **Opt-in raw-query eval-mining table** — the brief mentions "a separate opt-in table for eval
  mining." No consumer exists (F4 mines the git-versioned QA set, not prod queries), so it is
  **deferred (YAGNI)**. `request_logs` stays hash-only; when an eval-mining consumer is actually
  built, it gets its own opt-in, consented table and migration. Documented, not built.
- **Dashboards / alerting UI** — F14 (stats page) + F15 (prod dashboards) consume `/internal/stats`;
  F13 ships the endpoint, not the charts.
- **Relocating the cost table into `Settings`** — **Deviation from the brief's letter, on purpose.**
  The brief says "central per-1M-token price table in Settings." The table already lives centrally in
  `app/indexing/cost.py` and is imported by every feature; moving it into `Settings` is churn across
  six files with no behaviour change and a real risk of two tables drifting. F13 keeps `estimate_cost`
  as the single source (AC-11) and treats "central" as satisfied. If a reviewer insists on the literal
  Settings location, the one-line reconciliation is to have `cost.py` read a `Settings.MODEL_PRICES`
  dict — offered, not taken by default.
- **Per-request Langfuse for non-ask routes** — only the `/api/ask` chain gets a trace; auth/health
  are structlog-only.
