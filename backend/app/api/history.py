"""`GET /api/history` — the authed user's recent requests (F11, AC-5).

Reads `request_logs` (F13-owned write path). Build order is F11 → F13, so this endpoint is correct
but returns `[]` until F13 starts writing rows — that is an ordering fact, not a bug. Raw query text
is never returned: `request_logs` stores only `query_hash` by design (CLAUDE.md privacy rule).
"""

from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import get_current_user
from app.auth.schemas import Principal
from app.core.settings import settings
from app.db.models import RequestLog
from app.db.session import get_session

router = APIRouter(prefix="/api", tags=["history"])


class HistoryItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    request_id: str
    ts: datetime
    query_hash: str
    refused: bool
    cache_hit: bool
    degraded: bool
    total_ms: int | None
    model: str
    http_status: int


@router.get("/history", response_model=list[HistoryItem],
            summary="Recent requests for the authenticated user",
            description="The caller's last N request logs, newest first. Query text is hashed, "
                        "never returned. Empty until observability (F13) writes request_logs.")
async def history(
    principal: Principal = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[HistoryItem]:
    rows = (
        await session.execute(
            select(RequestLog)
            .where(RequestLog.user_id == principal.user_id)
            .order_by(RequestLog.ts.desc())
            .limit(settings.HISTORY_PAGE_SIZE)
        )
    ).scalars().all()
    return [HistoryItem.model_validate(r) for r in rows]
