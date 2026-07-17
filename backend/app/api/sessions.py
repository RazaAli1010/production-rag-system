"""Sessions REST router (F17, AC-1/3/4/5/6). Ownership is enforced by returning 404 (not 403) for a
session the caller does not own, so existence is never an enumeration oracle. Anonymous callers hold
a signed cookie carrying their session id; they may only touch the session that cookie names.

F11 later hardens this router (rate limit, validation middleware); F17 ships the memory-owning core.
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import get_current_user, get_current_user_optional
from app.auth.schemas import Principal
from app.core.ratelimit import rate_limit_dep
from app.core.settings import settings
from app.db.enums import MessageRole
from app.db.session import get_session
from app.memory import cookies, service

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


class SessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str | None
    total_tokens: int
    created_at: datetime
    last_active_at: datetime


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    role: MessageRole
    content: str
    refused: bool
    citations: list | None
    created_at: datetime


async def _resolve_owner(
    request: Request, session_id: uuid.UUID, principal: Principal | None, db: AsyncSession
):
    """Return the owned live session or raise 404. Authed → user_id must match; anon → the signed
    cookie must name exactly this session (AC-6/AC-8)."""
    if principal is not None:
        s = await service.get_owned(db, session_id, user_id=principal.user_id)
    else:
        cookie_sid = cookies.verify(request.cookies.get(cookies.COOKIE_NAME), settings=settings)
        if cookie_sid != session_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "session not found")
        s = await service.get_owned(db, session_id, user_id=None)
    if s is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "session not found")
    return s


@router.post("", response_model=SessionOut, status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(rate_limit_dep)])
async def create_session(
    response: Response,
    principal: Principal | None = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_session),
) -> SessionOut:
    user_id = principal.user_id if principal else None
    s = await service.create_session(db, user_id=user_id)
    if principal is None:
        # anonymous: issue the session id in a signed, httpOnly cookie (AC-1)
        response.set_cookie(
            cookies.COOKIE_NAME,
            cookies.sign(s.id, settings=settings),
            httponly=True,
            samesite="lax",
            max_age=settings.MEMORY_ANON_TTL_DAYS * 86_400,
        )
    return SessionOut.model_validate(s)


@router.get("", response_model=list[SessionOut])
async def list_sessions(
    principal: Principal = Depends(get_current_user),  # authed only (AC-3)
    db: AsyncSession = Depends(get_session),
) -> list[SessionOut]:
    rows = await service.list_sessions(db, user_id=principal.user_id)
    return [SessionOut.model_validate(r) for r in rows]


@router.get("/{session_id}/messages", response_model=list[MessageOut])
async def get_messages(
    session_id: uuid.UUID,
    request: Request,
    principal: Principal | None = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_session),
) -> list[MessageOut]:
    await _resolve_owner(request, session_id, principal, db)
    msgs = await service.get_messages(db, session_id)  # FULL transcript (AC-4)
    return [MessageOut.model_validate(m) for m in msgs]


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    session_id: uuid.UUID,
    request: Request,
    principal: Principal | None = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_session),
) -> Response:
    await _resolve_owner(request, session_id, principal, db)
    await service.archive(db, session_id)  # soft delete (AC-5)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
