# F13 — Observability · design.md

## 1. Where F13 sits (and what it reuses)

F13 is a **wiring feature**: the metrics, the request id, the table, the cost helper, and the
Langfuse handler all exist. F13 adds the four missing seams and nothing else.

```
already exists (do not rebuild)                          F13 adds
────────────────────────────────────────────────────    ─────────────────────────────
core.middleware.request_id_var (F11)                     observability/metrics.py
rag.observability.log_* + log_llm_cost (F3–F9)      ───▶   RequestMetrics accumulator (contextvar)
memory/stages.py Timer + emit (F17)                 ───▶   records stage ms into accumulator
rag.observability.langfuse_handler (F3, None-safe)  ───▶   + trace_id=request_id, tags=[APP_ENV]
db.models.RequestLog (F12, all columns)             ───▶   observability/request_log.py (write-behind)
memory/service.schedule_persist_assistant (F17)     ───▶   (same pattern, sibling task)
caching/keys.exact_key (F9)                          ──┐    query_hash reuse
indexing/cost.estimate_cost (F2)                     ──┴──▶  observability/stats.py (aggregations)
api/internal.py router (F10, admin-guarded)         ───▶   GET /internal/stats
structlog call sites everywhere (all)               ───▶   observability/logging.py configure_logging()
main.py lifespan                                    ───▶   one configure_logging(settings) call
```

## 2. Module layout

```
backend/app/observability/
├── __init__.py
├── metrics.py        # RequestMetrics dataclass + metrics_var contextvar + record helpers
├── request_log.py    # build_row(metrics, resp, http_status) + write_request_log() write-behind
├── logging.py        # configure_logging(settings) — the one structlog.configure call
└── stats.py          # gather_stats(db, window) -> StatsResponse (pure SQL aggregations)
```

Langfuse stays in `app/rag/observability.py` (extended in place, §5) — it is attached to the F3 chain
there; moving it would fork the one place that owns the callback. `app/observability/` is the new
home for the request-log + stats + logging config that no existing module owns.

## 3. Data flow (one `/api/ask` request)

```
RequestContextMiddleware (F11)          → sets request_id_var; F13 also resets metrics_var = RequestMetrics()
  │
  ├─ pipeline runs (baseline._pipeline_events, unchanged)
  │     each stage: memory/stages Timer.ms() ── record_stage(name, ms) ─▶ metrics_var
  │     each OpenAI call: log_llm_cost(...) ──── record_cost(tokens_in, tokens_out, model) ─▶ metrics_var
  │     langfuse_handler(session_id, settings) attached with trace_id=request_id (§5)
  │
  ├─ _collect / _sse_stream assembles AnswerResponse (flags, cache_hit, refused, degraded,
  │     memory_summarized, tokens, latency_ms) — the row's non-timing fields come straight from it
  │
  └─ AFTER response is ready:  asyncio.create_task(write_request_log(build_row(metrics, resp, status)))
         (sibling of F17's schedule_persist_assistant; never awaited on the response path — AC-4)
```

`record_stage` maps the F17 stage vocabulary to the `request_logs` columns:

| stage event            | request_logs column | notes |
|------------------------|---------------------|-------|
| `summarizing_memory`   | `summarize_ms`      | F17 |
| (memory load seam)     | `memory_ms`         | timed in service.load_context |
| `rewriting`            | `rewrite_ms`        | F7 |
| `cache_lookup`         | `embed_ms`          | lookup embeds the query; closest column |
| `searching`            | `retrieve_ms`       | F5 |
| `reranking`            | `rerank_ms`         | F6 |
| `generating`           | `llm_ms`            | F3 |
| `compressing`, `citing`| — (no column)       | stay in structlog / Langfuse only |

`total_ms` = the route's wall-clock (`AnswerResponse.latency_ms`, already measured by F11's `_stamp`).
Stages that ran are recorded; skipped stages leave their column `NULL` (nullable in F12 schema) — a
`NULL rerank_ms` *is* the signal that rerank was off, which stats reads via `pipeline_flags` anyway.

## 4. Function signatures

### `observability/metrics.py`
```python
from contextvars import ContextVar
from dataclasses import dataclass, field

@dataclass
class RequestMetrics:
    stage_ms: dict[str, int] = field(default_factory=dict)   # column_name -> ms
    tokens_in: int = 0
    tokens_out: int = 0
    est_cost_usd: float = 0.0
    model: str = ""

metrics_var: ContextVar[RequestMetrics | None] = ContextVar("request_metrics", default=None)

def record_stage(column: str, ms: int) -> None: ...   # no-op if metrics_var is None (non-ask route)
def record_cost(model: str, tokens_in: int, tokens_out: int) -> None:
    # accumulates tokens + estimate_cost(model, tokens_in, tokens_out); sets model
```
`record_*` read `metrics_var.get()`; when `None` (routes that never reset it) they no-op — so calling
them from shared pipeline code on a non-ask path is harmless. `estimate_cost` is imported from
`app.indexing.cost` (AC-11). All in-memory dict writes — pure CPU, runs inline (async-mandate: cheap
side of the line).

### `observability/request_log.py`
```python
def build_row(m: RequestMetrics, resp: AnswerResponse, *,
              user_id, session_id, channel, http_status: int,
              error_type: str | None, query_hash: str) -> dict: ...
    # maps m.stage_ms + resp fields onto the RequestLog columns; total_ms = resp.latency_ms

async def write_request_log(row: dict, sessionmaker) -> None:
    # opens its OWN async session (the request's session is closed by the time the task runs —
    # same reason F17's schedule_persist_assistant takes a sessionmaker), inserts one RequestLog,
    # commits; on error logs "observability.request_log_failed" and swallows (AC-4).
```
`query_hash = caching.keys.exact_key(normalized_query)` — computed by the ask route (which already
normalizes for the F9 cache key) and handed in, so hash and cache-entry hash agree (AC-5, enables the
stats join).

### `observability/logging.py`
```python
def configure_logging(settings) -> None:
    # structlog.configure(processors=[
    #   merge_contextvars,            # pulls in F11's request_id + bound APP_ENV
    #   add_log_level,
    #   TimeStamper(fmt="iso", utc=True),
    #   (JSONRenderer() if settings.LOG_JSON else ConsoleRenderer()),
    # ], wrapper_class=make_filtering_bound_logger(LOG_LEVEL), cache_logger_on_first_use=True)
    # binds APP_ENV once via bind_contextvars so every line carries the env tag.
```
Called once from `main.py` `_lifespan` startup (AC-7). Idempotent — safe under test re-config.

### `observability/stats.py`
```python
class StatsResponse(BaseModel):
    window: str; request_count: int
    p50_ms: int | None; p95_ms: int | None
    cache_hit_rate: float; refusal_rate: float; error_rate: float; degraded_rate: float
    total_cost_usd: float; tokens_saved_by_cache: int
    flag_usage: dict[str, int]; top_query_clusters: list[dict]
    active_sessions: int; mean_turns_per_session: float
    summarization_count: int; tokens_saved_by_summarization_est: int  # marked approximate

async def gather_stats(db: AsyncSession, window: timedelta) -> StatsResponse:
    # asyncio.gather of independent aggregate queries (all async, one AsyncSession):
    #  - counts/rates/p50/p95:  request_logs WHERE ts > now()-window
    #    (p50/p95 via percentile_cont within the window; SQL does it — no Python sort)
    #  - flag_usage:            jsonb_each over pipeline_flags, count where value=true
    #  - top_query_clusters:    cache_entries ORDER BY hits DESC LIMIT 10 (join query_hash for count)
    #  - tokens_saved_by_cache: sum over cache_entries of (answer->>'tokens_out')::int * hits
    #  - active_sessions / mean turns: sessions + messages WHERE last activity in window
    #  - summarization_count:   count(request_logs WHERE memory_summarized)
    #  - tokens_saved_by_summarization_est: summarization_count * MEMORY_SUMMARY_MAX_TOKENS
    #        (best-effort — the exact figure isn't a persisted column; labeled _est in the schema)
```

### `observability/__init__.py`
Re-exports `metrics_var`, `record_stage`, `record_cost`, `write_request_log`, `configure_logging`,
`gather_stats` so callers do `from app.observability import record_cost`.

## 5. Langfuse extension (in `app/rag/observability.py`, in place)

Current: `langfuse_handler(session_id, settings)`. F13 change is additive — read the request id from
the contextvar and pass the env tag:

```python
def langfuse_handler(session_id, settings):
    if not (settings.LANGFUSE_PUBLIC_KEY and settings.LANGFUSE_SECRET_KEY):
        return None                       # AC-2 unchanged
    from langfuse.callback import CallbackHandler
    return CallbackHandler(
        public_key=..., secret_key=..., host=settings.LANGFUSE_HOST,
        session_id=session_id,
        trace_name="ask",
        tags=[settings.APP_ENV],          # F13: env tag
        metadata={"request_id": request_id_var.get()},  # F13: correlation to request_logs / logs
    )
```
`trace_id` proper: the LangChain CallbackHandler derives the trace from the run; F13 sets
`metadata.request_id` (and `session_id`) as the join key rather than forcing Langfuse's internal id,
because the *correlation contract* is "grep the request_id" — that lands the id on the trace either
way. No new call site: `baseline.py` already calls `langfuse_handler(session_id, settings)` (line 294).

## 6. Error handling

- **Write-behind failure (AC-4):** `write_request_log` wraps its insert/commit in try/except, logs
  `observability.request_log_failed` with the request_id, and returns. A telemetry write must never
  surface to the user or crash the fired task. Losing one telemetry row is acceptable; losing the
  answer is not.
- **Handled request errors:** on a pipeline error the route still builds a row with
  `http_status=5xx`/`error_type=<slug>` from F11's exception taxonomy, so failures are counted in
  `error_rate`. (The row is written in the route's error path, same task mechanism.)
- **Stats query failure:** `/internal/stats` errors propagate as F11's normal 500 envelope — it is an
  admin read, not a hot path; no partial-result masking.
- **metrics_var unset:** `record_*` no-op (§4) — a pipeline call from a non-ask context never crashes.

## 7. New Settings keys (all in the one `Settings` class — AC-12)

```python
APP_ENV: str = "dev"                 # Langfuse env tag + structlog field; "prod" on Render
LOG_LEVEL: str = "INFO"
LOG_JSON: bool = True                # False → ConsoleRenderer for local dev readability
STATS_DEFAULT_WINDOW_H: int = 24     # GET /internal/stats default window
```
Reused, not redefined: `LANGFUSE_*` (F3), `HISTORY_PAGE_SIZE` (F11), `MEMORY_SUMMARY_MAX_TOKENS`
(memory), and the `estimate_cost` rate table (`app/indexing/cost.py`).

## 8. Alembic migrations

**None.** `request_logs` and `cache_entries` were migrated by F12 (`0001_initial`), `cache_entries`
got `query_hash` in F9 (`0003`). Every column F13 writes/reads already exists. `alembic revision
--autogenerate` produces an empty diff (AC-13) — a test asserts this stays empty.

## 9. Honoring the Shared Context contracts + F3 seam

- **`AnswerResponse` is the row's non-timing source** — F13 reads its `pipeline_flags, cache_hit,
  refused, degraded, memory_summarized, tokens_in/out, latency_ms, session_id, request_id` fields
  exactly as F11 populated them. No contract change, no new response field.
- **`StageEvent` is the timing source** — F13 reads the same `Timer.ms()` values the SSE `stage`
  events already carry; observability and the UI derive from one measurement, never two.
- **F3 retriever seam untouched** — F13 attaches no logic to retrieval; the Langfuse handler was
  already on the F3 chain, and the metrics accumulator only *reads* what the pipeline emits.
- **SSE contract untouched** — no new event type; F13's write happens after `done`, off-stream.
- **Privacy contract (CLAUDE.md)** — `request_logs.query_hash` only; `messages` raw text is a separate
  product table. The privacy test (AC-14) is the guard.

## 10. Testing strategy (feeds tasks.md)

- **Correlation test:** run `/api/ask` (Langfuse stubbed), assert exactly one `request_logs` row whose
  `request_id` == response `X-Request-ID`, and that a captured log line carries the same id.
- **Stats unit tests:** seed `request_logs`/`cache_entries`/`sessions`/`messages` with known rows,
  assert each `StatsResponse` field equals a hand-computed value; assert non-admin → 403.
- **Privacy test:** after an ask with a distinctive query string, assert that string is in no
  `request_logs` column and `query_hash == exact_key(normalized)`.
- **Graceful-Langfuse test:** `LANGFUSE_*` unset → ask runs green, row still written.
- **Write-behind test:** assert the response returns before the row exists (task not yet awaited),
  then the row appears after draining pending tasks — proves off-path (AC-4/15).
- **No-migration test:** `alembic revision --autogenerate` empty (reuse F11's assertion helper).
