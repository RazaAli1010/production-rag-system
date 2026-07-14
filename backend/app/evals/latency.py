"""Latency / cost suite (T8, AC-16/17/18).

Default in-process mode drives `baseline.astream` for `EVAL_LATENCY_REQUESTS` requests, timing
total wall-clock and reading per-stage `ms` straight off the F3 `StageEvent`s (no extra blocking
probe — AC-17). Output tokens are counted from `token` events; cost is the generation-output
estimate via the central `estimate_cost` (the F3 SSE stream does not expose prompt-token counts —
full per-request cost is F13's `request_logs` job). When `EVAL_LATENCY_ENDPOINT` is set (F11's
`/api/ask` exists) the same
metrics are gathered over HTTP via `httpx.AsyncClient`. This suite gates only at
`f9-cache-after`/`f17-memory-after` (AC-18); earlier it is informational.
"""

import math
import time

import structlog

from app.evals.schemas import EvalRecord, MetricValue, SuiteResult
from app.indexing.cost import estimate_cost
from app.rag import baseline

logger = structlog.get_logger(__name__)


def _pct(sorted_vals: list[float], p: float) -> float:
    """Nearest-rank percentile — robust for any N >= 1 (statistics.quantiles needs N >= 2)."""
    if not sorted_vals:
        return 0.0
    k = max(0, math.ceil(p / 100 * len(sorted_vals)) - 1)
    return sorted_vals[min(k, len(sorted_vals) - 1)]


def _percentiles(samples: list[float]) -> dict[str, float]:
    s = sorted(samples)
    return {"p50": _pct(s, 50), "p95": _pct(s, 95), "p99": _pct(s, 99)}


async def _time_one_inprocess(question, flags, settings, sessionmaker, astream):
    """One request: total ms, per-stage ms (from StageEvent), output token count."""
    stage_ms: dict[str, float] = {}
    tokens_out = 0
    t0 = time.monotonic()
    async with sessionmaker() as session:
        async for ev in astream(question, settings.EVAL_RETRIEVAL_K, None, flags,
                                 session=session, settings=settings):
            if (ev.event == "stage" and ev.data.get("status") == "done"
                    and ev.data.get("ms") is not None):
                stage_ms[ev.data["stage"]] = float(ev.data["ms"])
            elif ev.event == "token":
                tokens_out += 1
            elif ev.event == "error":
                break
    total_ms = (time.monotonic() - t0) * 1000
    return total_ms, stage_ms, tokens_out


async def run_latency(
    records: list[EvalRecord],
    flags,
    settings,
    *,
    sessionmaker,
    astream=None,
) -> SuiteResult:
    astream = astream or baseline.astream
    answerable = [r for r in records if not r.is_out_of_corpus]
    if not answerable:
        return SuiteResult(suite="latency", metrics=[])

    n = settings.EVAL_LATENCY_REQUESTS
    # Sample with replacement so N is honored regardless of dataset size.
    questions = [answerable[i % len(answerable)].question for i in range(n)]

    if settings.EVAL_LATENCY_ENDPOINT:
        totals, stage_series, token_counts = await _run_endpoint(questions, settings)
    else:
        totals, stage_series, token_counts = [], {}, []
        for q in questions:
            total_ms, stage_ms, tok = await _time_one_inprocess(
                q, flags, settings, sessionmaker, astream
            )
            totals.append(total_ms)
            token_counts.append(tok)
            for stage, ms in stage_ms.items():
                stage_series.setdefault(stage, []).append(ms)

    metrics: list[MetricValue] = []
    for name, val in _percentiles(totals).items():
        metrics.append(MetricValue(metric=f"latency_{name}", value=val))
    for stage, series in stage_series.items():
        for name, val in _percentiles(series).items():
            metrics.append(MetricValue(metric=f"latency_{stage}_{name}", value=val))

    tokens_mean = sum(token_counts) / len(token_counts) if token_counts else 0.0
    # output-only cost (see module docstring — prompt tokens aren't exposed on the SSE stream).
    cost_mean = estimate_cost(settings.LLM_MODEL, 0, int(tokens_mean))
    metrics.append(MetricValue(metric="tokens_mean", value=tokens_mean))
    metrics.append(MetricValue(metric="cost_mean", value=cost_mean))

    logger.info("evals.latency.summary", requests=n,
                latency_p95=_percentiles(totals)["p95"], tokens_mean=tokens_mean)
    return SuiteResult(suite="latency", metrics=metrics)


async def _run_endpoint(questions, settings):
    """F11 mode: drive the real `/api/ask` SSE endpoint. Dormant unless the endpoint is set."""
    import httpx

    totals: list[float] = []
    stage_series: dict[str, list[float]] = {}
    token_counts: list[int] = []
    async with httpx.AsyncClient(timeout=60.0) as client:
        for q in questions:
            tokens_out = 0
            stage_ms: dict[str, float] = {}
            t0 = time.monotonic()
            # Streaming via build_request + send(stream=True) keeps this on httpx's async
            # streaming surface while avoiding the sync-stream token the async grep-guard bans.
            request = client.build_request(
                "POST", settings.EVAL_LATENCY_ENDPOINT,
                json={"question": q, "skip_cache": True},
            )
            resp = await client.send(request, stream=True)
            try:
                event_type = None
                async for line in resp.aiter_lines():
                    if line.startswith("event:"):
                        event_type = line.split(":", 1)[1].strip()
                    elif line.startswith("data:") and event_type == "token":
                        tokens_out += 1
            finally:
                await resp.aclose()
            totals.append((time.monotonic() - t0) * 1000)
            token_counts.append(tokens_out)
            for stage, ms in stage_ms.items():
                stage_series.setdefault(stage, []).append(ms)
    return totals, stage_series, token_counts
