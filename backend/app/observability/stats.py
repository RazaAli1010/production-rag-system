"""Admin stats aggregation (F13, AC-10).

Every figure is a SQL aggregate over the tables earlier features already own — `request_logs`
(F13's own writes), `cache_entries` (F9), `sessions`/`messages` (F17). No new state.

Queries run sequentially on the one request session: asyncpg allows a single operation per
connection at a time, so `asyncio.gather` over a shared session would error — and a rarely-hit admin
endpoint has no need to parallelise a handful of index-backed aggregates.
"""

from datetime import UTC, datetime, timedelta

import structlog
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)


class StatsResponse(BaseModel):
    window: str
    request_count: int
    p50_ms: int | None
    p95_ms: int | None
    cache_hit_rate: float
    refusal_rate: float
    error_rate: float
    degraded_rate: float
    total_cost_usd: float
    tokens_saved_by_cache: int
    flag_usage: dict[str, int]
    top_query_clusters: list[dict]
    active_sessions: int
    mean_turns_per_session: float
    summarization_count: int
    tokens_saved_by_summarization_est: int  # rough: summarized turns × summary budget


_ROLLUP = text("""
    SELECT count(*)                                             AS n,
           percentile_cont(0.5)  WITHIN GROUP (ORDER BY total_ms) AS p50,
           percentile_cont(0.95) WITHIN GROUP (ORDER BY total_ms) AS p95,
           coalesce(avg(cache_hit::int), 0)                    AS cache_hit_rate,
           coalesce(avg(refused::int), 0)                      AS refusal_rate,
           coalesce(avg((http_status >= 500)::int), 0)         AS error_rate,
           coalesce(avg(degraded::int), 0)                     AS degraded_rate,
           coalesce(sum(est_cost_usd), 0)                      AS total_cost,
           count(*) FILTER (WHERE memory_summarized)           AS summ_count
    FROM request_logs WHERE ts > :since
""")

_FLAGS = text("""
    SELECT key, count(*) FILTER (WHERE value = 'true'::jsonb) AS n
    FROM request_logs, jsonb_each(pipeline_flags)
    WHERE ts > :since
    GROUP BY key
""")

# cache_entries.hits is cumulative (a hit doesn't carry a timestamp), so these are lifetime figures,
# independent of the window — the honest reading of "tokens saved by cache" and "top clusters".
_CLUSTERS = text("""
    SELECT query_text, hits FROM cache_entries ORDER BY hits DESC LIMIT 10
""")
_CACHE_SAVED = text("""
    SELECT coalesce(sum(hits * coalesce((answer->>'tokens_out')::int, 0)), 0) AS saved
    FROM cache_entries
""")

# mean_turns_per_session counts messages (user + assistant) per session active in the window.
_SESSIONS = text("""
    SELECT count(*)                                    AS active,
           coalesce(avg(mc.cnt), 0)                    AS mean_turns
    FROM sessions s
    LEFT JOIN (SELECT session_id, count(*) AS cnt FROM messages GROUP BY session_id) mc
           ON mc.session_id = s.id
    WHERE s.last_active_at > :since
""")


async def gather_stats(db: AsyncSession, window: timedelta, *, summary_budget: int) -> StatsResponse:
    since = datetime.now(UTC) - window
    p = {"since": since}

    r = (await db.execute(_ROLLUP, p)).one()
    flags = {row.key: row.n for row in (await db.execute(_FLAGS, p)).all()}
    clusters = [{"query": row.query_text, "hits": row.hits}
                for row in (await db.execute(_CLUSTERS)).all()]
    saved = (await db.execute(_CACHE_SAVED)).scalar_one()
    s = (await db.execute(_SESSIONS, p)).one()

    return StatsResponse(
        window=_fmt_window(window),
        request_count=r.n,
        p50_ms=int(r.p50) if r.p50 is not None else None,
        p95_ms=int(r.p95) if r.p95 is not None else None,
        cache_hit_rate=float(r.cache_hit_rate),
        refusal_rate=float(r.refusal_rate),
        error_rate=float(r.error_rate),
        degraded_rate=float(r.degraded_rate),
        total_cost_usd=float(r.total_cost),
        tokens_saved_by_cache=int(saved),
        flag_usage=flags,
        top_query_clusters=clusters,
        active_sessions=s.active,
        mean_turns_per_session=float(s.mean_turns),
        summarization_count=r.summ_count,
        tokens_saved_by_summarization_est=r.summ_count * summary_budget,
    )


def _fmt_window(window: timedelta) -> str:
    hours = int(window.total_seconds() // 3600)
    return f"{hours}h"
