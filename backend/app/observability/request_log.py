"""The `request_logs` write path (F13, AC-3/4/5).

One row per `/api/ask`, written write-behind — mirrors F17's `schedule_persist_assistant`
(`app/memory/service.py`): own short-lived session (the request's is closed by the time the task
runs), every error swallowed (a telemetry write must never surface to the client), strong task
reference so it isn't GC'd mid-await.

The row is assembled from `meta` (the AnswerResponse-sans-answer dict the ask route already builds —
it carries flags/tokens/cache/refused/degraded/memory_summarized/latency) plus the request metrics
accumulator (stage timings + real spend) plus a few route-known values. Raw query text is never
written: only `query_hash = exact_key(normalize(question))` (AC-5).
"""

import asyncio
import uuid

import structlog

from app.core.middleware import request_id_var
from app.db.models import RequestLog
from app.observability.metrics import RequestMetrics

logger = structlog.get_logger(__name__)

_MS_COLUMNS = ("embed_ms", "retrieve_ms", "rerank_ms", "rewrite_ms",
               "memory_ms", "summarize_ms", "llm_ms")

# Strong refs so a fired task isn't GC'd mid-await (the create_task footgun, per memory/service.py).
_WRITE_TASKS: set[asyncio.Task] = set()


def build_row(
    meta: dict | None,
    metrics: RequestMetrics,
    *,
    user_id: uuid.UUID | None,
    session_id: uuid.UUID | None,
    channel,
    model: str,
    http_status: int,
    error_type: str | None,
    query_hash: str,
) -> dict:
    """Map `meta` + `metrics` onto RequestLog columns. `meta is None` = a hard error before the
    pipeline emitted `meta` (timeout / provider fail): fill telemetry defaults so the row still
    counts toward error_rate."""
    meta = meta or {}
    row = {
        "request_id": meta.get("request_id") or request_id_var.get(),
        "user_id": user_id,
        "session_id": session_id,
        "channel": channel,
        "query_hash": query_hash,
        "pipeline_flags": meta.get("pipeline_flags") or {},
        "cache_hit": meta.get("cache_hit", False),
        "refused": meta.get("refused", False),
        "degraded": meta.get("degraded", False),
        "memory_summarized": meta.get("memory_summarized", False),
        "total_ms": meta.get("latency_ms"),
        "tokens_in": meta.get("tokens_in", 0),
        "tokens_out": meta.get("tokens_out", 0),
        "est_cost_usd": metrics.est_cost_usd,
        "model": model,
        "http_status": http_status,
        "error_type": error_type,
    }
    for col in _MS_COLUMNS:
        row[col] = metrics.stage_ms.get(col)
    return row


async def _write_guarded(row: dict, *, sessionmaker) -> None:
    try:
        async with sessionmaker() as db:
            db.add(RequestLog(**row))
            await db.commit()
    except Exception as exc:  # noqa: BLE001 — a telemetry write is never fatal
        logger.warning("observability.request_log_failed",
                       request_id=row.get("request_id"), error=str(exc))


def schedule_request_log(row: dict, *, sessionmaker) -> asyncio.Task:
    """Fire-and-forget the row write off the response path (AC-4). Returns the task so tests drain."""
    task = asyncio.create_task(_write_guarded(row, sessionmaker=sessionmaker))
    _WRITE_TASKS.add(task)
    task.add_done_callback(_WRITE_TASKS.discard)
    return task


async def drain_writes() -> None:
    """Await in-flight request_log writes — tests only, never the request path."""
    if _WRITE_TASKS:
        await asyncio.gather(*list(_WRITE_TASKS), return_exceptions=True)
