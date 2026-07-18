# F13 — Observability · tasks.md

Ordered, each ≤ ~1h, each with a test criterion. F13 is a wiring feature — most tasks are small
because the metrics, table, cost helper, and request id already exist. No Alembic revision, no new
eval label (AC-15).

---

### T1 — Settings keys
Add `APP_ENV="dev"`, `LOG_LEVEL="INFO"`, `LOG_JSON=True`, `STATS_DEFAULT_WINDOW_H=24` to the one
`Settings` class (design §7). Confirm `LANGFUSE_*`, `HISTORY_PAGE_SIZE`, `MEMORY_SUMMARY_MAX_TOKENS`
are reused, not redefined.
**Test:** `Settings()` loads with defaults; env override of `APP_ENV` reflects. (AC-12)

### T2 — Central structlog config
Add `observability/logging.py::configure_logging(settings)` (design §4): `merge_contextvars`,
`add_log_level`, ISO `TimeStamper`, `JSONRenderer`/`ConsoleRenderer` by `LOG_JSON`, level filter;
bind `APP_ENV`. Call it once in `main.py` `_lifespan` startup. Remove any ad-hoc config.
**Test:** a captured log line renders as JSON and carries `request_id` (set via contextvar) + `env`. (AC-7/9)

### T3 — Request metrics accumulator
Add `observability/metrics.py`: `RequestMetrics` dataclass, `metrics_var` contextvar, `record_stage`,
`record_cost` (uses `indexing.cost.estimate_cost`) — all no-op when `metrics_var` is `None` (design §4).
Reset `metrics_var = RequestMetrics()` in `RequestContextMiddleware` (F11) alongside the request-id set.
**Test:** `record_cost` accumulates tokens + cost into the contextvar; called with `metrics_var=None`
it no-ops without raising. (AC-3/11)

### T4 — Feed the accumulator from existing seams
In `memory/stages.py` (or the `Timer` callers) call `record_stage(column, ms)` on each `done` span,
using the stage→column map (design §3). In `rag.observability.log_llm_cost` (and the summarizer's
cost log) call `record_cost(...)`. These are additive one-liners at seams that already compute the
values — no new timing, no new call sites.
**Test:** after a stubbed pipeline run, `metrics_var.get()` holds the expected `stage_ms` keys and
token totals. (AC-3)

### T5 — request_logs write-behind
Add `observability/request_log.py`: `build_row(...)` mapping `RequestMetrics` + `AnswerResponse` onto
`RequestLog` columns (`total_ms=resp.latency_ms`, `query_hash=exact_key(normalized)`), and
`write_request_log(row, sessionmaker)` opening its own async session, inserting one row, committing,
swallowing+logging on failure (design §4/§6).
**Test:** `write_request_log` inserts exactly one row with correct fields; a forced insert error is
logged, not raised. (AC-3/4/5)

### T6 — Wire the write into the ask route
In `api/ask.py`, after the response is assembled (both SSE `_memory_events`/`_stateless_events` clean
end and JSON `_collect`), fire `asyncio.create_task(write_request_log(build_row(...), get_sessionmaker()))`
— sibling of F17's `schedule_persist_assistant`. Populate `channel`, `user_id`, `session_id`,
`http_status`, `error_type` (null on success; set on the error path). Ask-only (AC-6).
**Test:** an `/api/ask` run writes exactly one row whose `request_id` == response `X-Request-ID`; a
`/api/health` call writes none. (AC-3/6)

### T7 — Langfuse trace_id + env tag
Extend `rag.observability.langfuse_handler` (design §5): add `tags=[settings.APP_ENV]`,
`metadata={"request_id": request_id_var.get()}`, `trace_name="ask"`. Keep the None-safe guard.
**Test:** with keys set, handler carries the env tag + request_id metadata; with a key unset it
returns `None` and the pipeline runs with no callback. (AC-1/2)

### T8 — Stats aggregation
Add `observability/stats.py`: `StatsResponse` model + `gather_stats(db, window)` running the AC-10
aggregates as async SQL over `request_logs`/`cache_entries`/`sessions`/`messages` (p50/p95 via
`percentile_cont`, flag counts via `jsonb_each`, cache savings via `cache_entries.hits`, summarization
est via `count × MEMORY_SUMMARY_MAX_TOKENS`), `asyncio.gather`-ed (design §4).
**Test:** seeded fixtures → every `StatsResponse` field equals a hand-computed expected value. (AC-10)

### T9 — /internal/stats endpoint
Add `GET /internal/stats?window=<Nh|Nd>` to `api/internal.py` (already admin-guarded), defaulting to
`STATS_DEFAULT_WINDOW_H`, parsing the window to a `timedelta`, returning `gather_stats(...)`. Add its
OpenAPI schema + example.
**Test:** admin gets 200 + populated `StatsResponse`; non-admin gets 403 (router guard); bad `window`
→ 422. (AC-10)

### T10 — Privacy test
Assert that after an `/api/ask` run with a distinctive query, that string appears in no `request_logs`
column and `query_hash == exact_key(normalized_query)`; assert `messages` (F17) *does* hold the raw
text (the deliberate product/telemetry distinction).
**Test:** privacy test green. (AC-14)

### T11 — No-migration + async guards
Run `alembic revision --autogenerate` → assert empty (reuse F11's helper). Confirm the CI
async/sync-twin grep passes over `app/observability` (async session, no blocking calls).
**Test:** empty autogenerate diff; CI async check green. (AC-13/12)

### T12 — Observability gate (DoD)
No new eval label (AC-15 — F13 changes no retrieval/generation). Prove the feature-level DoD from
`requirements.md §4`:
1. one `request_id` → Langfuse trace (stubbed) + `request_logs` row + correlated log lines;
2. `/internal/stats` returns every AC-10 field, unit-tested on seeded fixtures; non-admin 403;
3. privacy test passes (hash present, raw text absent);
4. `LANGFUSE_*` unset → ask still green + row still written;
5. `alembic revision --autogenerate` empty;
6. write-behind overhead sanity check — the ask response returns before the row task is awaited
   (added p50 ≈ 0); note this in the PR description (no new `docs/eval_results/` delta file, since
   there is no hit@k/latency label — consistent with F11's production-layer precedent).

**Definition of done = all six above green.** (AC-1..AC-15)
