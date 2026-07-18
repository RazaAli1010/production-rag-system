"""Per-request metrics accumulator (F13, AC-3).

The pipeline already computes every number as a structlog event; F13 collects the two things `meta`
does NOT already carry — the per-stage timings and the real OpenAI spend — into one request-scoped
object, so the write-behind `request_logs` row (design §3) can read them once at the end.

`metrics_var` is reset per request by `RequestContextMiddleware`. The `record_*` helpers no-op when
it is `None`, so shared pipeline code (`stages.emit`, `log_llm_cost`) can call them unconditionally —
a non-ask path (a bare task, a health check) simply has no accumulator and drops the record.

Async-mandate: pure in-memory dict/float writes over a handful of values — inline (the cheap side of
the line, like `estimate_cost` itself).
"""

from contextvars import ContextVar
from dataclasses import dataclass, field

from app.indexing.cost import estimate_cost

# stage name (F17 vocabulary) → request_logs column. Stages without a column (compressing, citing)
# are absent here and stay in structlog/Langfuse only (design "Key decisions").
STAGE_COLUMN = {
    "summarizing_memory": "summarize_ms",
    "rewriting": "rewrite_ms",
    "cache_lookup": "embed_ms",
    "searching": "retrieve_ms",
    "reranking": "rerank_ms",
    "generating": "llm_ms",
}


@dataclass
class RequestMetrics:
    stage_ms: dict[str, int] = field(default_factory=dict)  # request_logs column -> ms
    est_cost_usd: float = 0.0  # real OpenAI spend this request (0 on a cache hit)


metrics_var: ContextVar[RequestMetrics | None] = ContextVar("request_metrics", default=None)


def record_stage(stage: str, ms: int) -> None:
    """Map an F17 stage `done` span onto its request_logs column. No-op off an ask path."""
    m = metrics_var.get()
    if m is None:
        return
    column = STAGE_COLUMN.get(stage)
    if column is not None:
        m.stage_ms[column] = ms


def record_cost(model: str, tokens_in: int, tokens_out: int) -> None:
    """Accumulate real spend across every OpenAI call in the request. No-op off an ask path."""
    m = metrics_var.get()
    if m is None:
        return
    m.est_cost_usd += estimate_cost(model, tokens_in, tokens_out)
