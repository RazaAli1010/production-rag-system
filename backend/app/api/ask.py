"""`POST /api/ask` — the session-aware SSE endpoint (F17). Owns memory binding, the per-session
lock (409 `session_busy`), user-write-first + assistant write-behind, and the `summarizing_memory`
stage. Delegates the actual RAG to `rag.baseline.astream` unchanged.

Memory off OR no `session_id` → stateless single turn, byte-for-byte `f9-cache-after` (AC-33). F11
later wraps this with rate-limit/validation/request-log middleware — out of F17 scope.
"""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import get_current_user_optional
from app.auth.schemas import Principal
from app.core.contracts import AnswerResponse
from app.core.settings import settings
from app.db.engine import get_sessionmaker
from app.db.session import get_session
from app.memory import cookies, service, stages, summarizer
from app.rag.baseline import astream
from app.rag.events import SSEEvent

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api", tags=["ask"])


class AskRequest(BaseModel):
    question: str = Field(min_length=1)
    session_id: uuid.UUID | None = None


def _encode(ev: SSEEvent) -> str:
    """One SSE frame: `event:`/`data:` lines ending in a blank line (F14's wire contract)."""
    return f"event: {ev.event}\ndata: {json.dumps(ev.data)}\n\n"


async def _stateless_events(question: str, db: AsyncSession) -> AsyncIterator[SSEEvent]:
    """No session → the pre-F17 single-turn stream (AC-33). `sessionmaker` is only used by F9's
    cache (off in this path unless ENABLE_CACHE); pass the app-wide one so it still works."""
    async for ev in astream(question, session=db, settings=settings,
                            sessionmaker=get_sessionmaker()):
        yield ev


async def _run_summary_stage(db, session, recent_pending) -> None:
    """Fold the pending slid-out pairs into the rolling summary (AC-23/25/27). Never blocks the
    answer: a summariser failure logs `memory.summarize_failed` and proceeds window-only."""
    pending = recent_pending
    try:
        new_summary = await asyncio.wait_for(
            summarizer.extend_summary(session.summary, pending, settings),
            timeout=settings.MEMORY_SUMMARY_TIMEOUT_S,
        )
        await service.apply_summary(db, session, new_summary, upto_id=pending[-1].id)
    except Exception as exc:  # noqa: BLE001 — summary is best-effort; pending stays pending, retried
        logger.warning("memory.summarize_failed", session_id=str(session.id), error=str(exc))


async def _memory_events(
    question: str, session, db: AsyncSession, lock: asyncio.Lock
) -> AsyncIterator[SSEEvent]:
    """The memory-on turn. Holds `lock` for the whole stream and releases it in `finally` — the lock
    was already acquired synchronously by the endpoint, so this generator owns its release."""
    try:
        # 1) user message FIRST (AC-10) — its created_at sorts before the assistant write-behind.
        await service.persist_user(db, session, question, settings)

        # 2) memory load + lazy summary (before retrieval, AC-23).
        recent, pending = await service.load_memory(db, session, settings)
        if pending is not None:
            timer = stages.Timer()
            yield stages.emit(stages.MEMORY_STAGE, "started")
            await _run_summary_stage(db, session, pending)
            yield stages.emit(stages.MEMORY_STAGE, "done", ms=timer.ms())
        else:
            yield stages.emit(stages.MEMORY_STAGE, "skipped")

        # Assemble the context while the ORM `session` is still live, THEN commit — committing here
        # (not at request end) releases the sessions-row lock persist_user/apply_summary took, so
        # the assistant write-behind's atomic increment can't deadlock against a lock held for the
        # whole stream. The user question is also durable now, which is what AC-10 intends.
        ctx = service.assemble(session, recent, settings)
        await db.commit()

        # 3) the F3 pipeline, unchanged. Accumulate answer text + meta to reconstruct the response.
        answer_text = ""
        meta = None
        clean_done = False
        async for ev in astream(question, memory=ctx, session=db, settings=settings,
                                sessionmaker=get_sessionmaker(), session_id=str(session.id)):
            if ev.event == "token":
                answer_text += ev.data["token"]
            elif ev.event == "meta":
                meta = ev.data
            elif ev.event == "done":
                clean_done = True
            yield ev

        # 4) assistant write-behind — ONLY on a clean `done` (AC-11). A mid-stream disconnect
        # cancels this generator before `done`, so this line is never reached and no partial answer
        # persists (AC-12).
        if clean_done and meta is not None:
            resp = AnswerResponse(answer=answer_text, **meta)
            service.schedule_persist_assistant(session.id, resp, sessionmaker=get_sessionmaker())
    finally:
        lock.release()


@router.post("/ask")
async def ask(
    req: AskRequest,
    request: Request,
    principal: Principal | None = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    async def _sse(gen: AsyncIterator[SSEEvent]) -> AsyncIterator[str]:
        async for ev in gen:
            yield _encode(ev)

    # memory off / no session → stateless (AC-33)
    if not settings.ENABLE_MEMORY or req.session_id is None:
        return StreamingResponse(_sse(_stateless_events(req.question, db)),
                                 media_type="text/event-stream")

    # memory on → resolve ownership (404 on foreign/missing, AC-6/9)
    session = await _resolve_owned(request, req.session_id, principal, db)

    # per-session serialisation (AC-31). Check + acquire is atomic: asyncio.Lock's free-path acquire
    # does not suspend, so no other coroutine runs between `locked()` and `acquire()`.
    lock = service.lock_for(session.id)
    if lock.locked():
        raise HTTPException(status.HTTP_409_CONFLICT, detail="session_busy")
    await lock.acquire()
    return StreamingResponse(_sse(_memory_events(req.question, session, db, lock)),
                             media_type="text/event-stream")


async def _resolve_owned(request: Request, session_id: uuid.UUID, principal, db):
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
