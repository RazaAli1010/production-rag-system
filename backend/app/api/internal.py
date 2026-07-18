import re
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import require_role
from app.core.settings import settings
from app.db.session import get_session
from app.observability.stats import StatsResponse, gather_stats

# Router-level guard, so every endpoint F13 hangs here (stats, cache flush, doc status, eval
# results) is admin-only by default rather than by remembering.
router = APIRouter(
    prefix="/internal",
    tags=["internal"],
    dependencies=[Depends(require_role("admin"))],
)

_WINDOW_RE = re.compile(r"^(\d+)([hd])$")


def _parse_window(window: str) -> timedelta:
    """`<N>h` or `<N>d` → timedelta. Anything else is a 422 (AC-10)."""
    m = _WINDOW_RE.match(window)
    if not m:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="window must look like '24h' or '7d'")
    n, unit = int(m.group(1)), m.group(2)
    if n == 0:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="window must be > 0")
    return timedelta(hours=n) if unit == "h" else timedelta(days=n)


@router.get("/ping")
async def ping() -> dict:
    return {"ok": True}


@router.get("/stats", response_model=StatsResponse, summary="Aggregate request stats (admin)",
            description="Counts, p50/p95 latency, cache/refusal/error/degraded rates, cost, tokens "
                        "saved, per-flag usage, top query clusters, and session/summary stats over "
                        "the given window (default from STATS_DEFAULT_WINDOW_H).")
async def stats(
    window: str = Query(default=None, examples=["24h"]),
    db: AsyncSession = Depends(get_session),
) -> StatsResponse:
    td = _parse_window(window) if window else timedelta(hours=settings.STATS_DEFAULT_WINDOW_H)
    return await gather_stats(db, td, summary_budget=settings.MEMORY_SUMMARY_MAX_TOKENS)
