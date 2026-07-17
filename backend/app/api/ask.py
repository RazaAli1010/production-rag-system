"""`POST /api/ask` — the session-aware SSE endpoint (F17), hardened for production (F11).

F17 owns the core: memory binding, the per-session lock (409 `session_busy`), user-write-first +
assistant write-behind, and the `summarizing_memory` stage — all unchanged here. F11 adds the public
surface around it:

- input contract (question 3–500, namespace/deep/skip_cache/flags_override) + validation;
- the effective `PipelineFlags` built from `Settings` (F17 shipped the route with NO flags, so
  `apply_flags` was forcing every enhancement off — F11 makes deployed `ENABLE_*` take effect);
- `deep=true` → the `gpt-4o` deep model via a settings copy (no baseline.py change);
- content negotiation: SSE by default, one JSON `AnswerResponse` on `Accept: application/json`;
- `request_id` + `latency_ms` stamped onto `meta`/the JSON body;
- a `REQUEST_TIMEOUT_S` server-side timeout → terminal SSE `error` (JSON → 504);
- per-tier Redis rate limiting via `rate_limit_dep`.
"""

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import aclosing

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import get_current_user_optional
from app.auth.schemas import Principal
from app.core.contracts import AnswerResponse, PipelineFlags
from app.core.middleware import request_id_var
from app.core.ratelimit import rate_limit_dep
from app.core.settings import settings
from app.db.engine import get_sessionmaker
from app.db.session import get_session
from app.memory import cookies, service, stages, summarizer
from app.rag.baseline import astream
from app.rag.errors import ProviderError
from app.rag.events import SSEEvent

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api", tags=["ask"])


class AskRequest(BaseModel):
    # 3–500 is the frozen public wire contract (F14 builds from it), so it lives in the schema — not
    # Settings — exactly like the {doc_id}:{seq} id format.
    question: str = Field(min_length=3, max_length=500,
                          examples=["probation se kaise nikalta hoon?"])
    session_id: uuid.UUID | None = None
    namespace: str | None = Field(default=None, pattern="^(pu|hec)$", examples=["pu"])
    deep: bool = False
    skip_cache: bool = False
    # Admin-only pipeline override, e.g. {"hybrid": true, "rerank": false}. Keys validated against
    # PipelineFlags; a non-admin caller sending this gets 403.
    flags_override: dict[str, bool] | None = None


def _encode(ev: SSEEvent) -> str:
    """One SSE frame: `event:`/`data:` lines ending in a blank line (F14's wire contract)."""
    return f"event: {ev.event}\ndata: {json.dumps(ev.data)}\n\n"


def _flags_for_request(req: AskRequest, principal: Principal | None) -> PipelineFlags:
    """The effective pipeline toggles for THIS request: deployed `ENABLE_*` defaults, `skip_cache`
    applied to the cache toggle, then an admin `flags_override` on top (AC-1/4).

    This is the seam CLAUDE.md's "flags checked at each seam" refers to — without it, `astream`
    receives no flags and `apply_flags` overlays an all-False `PipelineFlags()`, disabling every
    enhancement regardless of Settings.
    """
    flags = PipelineFlags(
        hybrid=settings.ENABLE_HYBRID,
        rerank=settings.ENABLE_RERANK,
        query_rewrite=settings.ENABLE_QUERY_REWRITE,
        compression=settings.ENABLE_COMPRESSION,
        cache=settings.ENABLE_CACHE and not req.skip_cache,
        memory=settings.ENABLE_MEMORY,
    )
    if req.flags_override:
        if principal is None or principal.kind != "admin":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "flags_override requires admin")
        unknown = set(req.flags_override) - set(PipelineFlags.model_fields)
        if unknown:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                f"unknown flags: {sorted(unknown)}")
        flags = flags.model_copy(update=req.flags_override)
    return flags


def _pipeline_settings(req: AskRequest):
    """`deep=true` swaps in the deep-mode model without touching the pipeline —
    `build_llm(settings)` reads `LLM_MODEL` (AC-3)."""
    if req.deep:
        return settings.model_copy(update={"LLM_MODEL": settings.LLM_DEEP_MODEL})
    return settings


async def _stateless_events(
    question: str, req: AskRequest, flags: PipelineFlags, db: AsyncSession
) -> AsyncIterator[SSEEvent]:
    """No session → the single-turn stream (AC-33 parity for the memory-off case)."""
    async for ev in astream(question, namespace=req.namespace, flags=flags, session=db,
                            settings=_pipeline_settings(req), sessionmaker=get_sessionmaker()):
        yield ev


async def _run_summary_stage(db, session, recent_pending) -> None:
    """Fold the pending slid-out pairs into the rolling summary (F17 AC-23/25/27). Never blocks the
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
    question: str, req: AskRequest, flags: PipelineFlags, session, db: AsyncSession,
    lock: asyncio.Lock,
) -> AsyncIterator[SSEEvent]:
    """The memory-on turn (F17). Holds `lock` for the whole stream and releases it in `finally`."""
    try:
        # 1) user message FIRST (F17 AC-10) — created_at sorts before the assistant write-behind.
        await service.persist_user(db, session, question, settings)

        # 2) memory load + lazy summary (before retrieval, F17 AC-23).
        recent, pending = await service.load_memory(db, session, settings)
        if pending is not None:
            timer = stages.Timer()
            yield stages.emit(stages.MEMORY_STAGE, "started")
            await _run_summary_stage(db, session, pending)
            yield stages.emit(stages.MEMORY_STAGE, "done", ms=timer.ms())
        else:
            yield stages.emit(stages.MEMORY_STAGE, "skipped")

        ctx = service.assemble(session, recent, settings)
        await db.commit()

        # 3) the F3 pipeline. Accumulate answer text + meta to reconstruct the response.
        answer_text = ""
        meta = None
        clean_done = False
        async for ev in astream(question, namespace=req.namespace, flags=flags, memory=ctx,
                                session=db, settings=_pipeline_settings(req),
                                sessionmaker=get_sessionmaker(), session_id=str(session.id)):
            if ev.event == "token":
                answer_text += ev.data["token"]
            elif ev.event == "meta":
                meta = ev.data
            elif ev.event == "done":
                clean_done = True
            yield ev

        # 4) assistant write-behind — ONLY on a clean `done` (F17 AC-11). A mid-stream disconnect or
        # timeout cancels this generator before `done`, so this line is never reached (AC-12/18).
        if clean_done and meta is not None:
            resp = AnswerResponse(answer=answer_text, **meta)
            service.schedule_persist_assistant(session.id, resp, sessionmaker=get_sessionmaker())
    except asyncio.CancelledError:
        logger.info("api.client_disconnect", session_id=str(session.id))
        raise
    finally:
        lock.release()


def _stamp(data: dict, started: float) -> dict:
    """Inject the correlation id + server wall-clock onto a `meta` payload (AC-14)."""
    data["request_id"] = request_id_var.get()
    data["latency_ms"] = int((time.monotonic() - started) * 1000)
    return data


async def _sse_stream(gen: AsyncIterator[SSEEvent], started: float) -> AsyncIterator[str]:
    """Serialize events to SSE frames, stamping `meta`, enforcing the server timeout as a terminal
    `error` event (the 200 stream has already started, so status can't change), and closing `gen`
    so its `finally` (lock release) always runs (AC-17/18)."""
    try:
        async with aclosing(gen):
            async with asyncio.timeout(settings.REQUEST_TIMEOUT_S):
                async for ev in gen:
                    if ev.event == "meta":
                        _stamp(ev.data, started)
                    yield _encode(ev)
    except TimeoutError:
        logger.warning("api.timeout")
        yield _encode(SSEEvent(event="error", data={"message": "request timed out"}))


async def _collect(gen: AsyncIterator[SSEEvent], started: float) -> AnswerResponse:
    """JSON variant: drain the same generator (so lock + write-behind still run), rebuild the
    `AnswerResponse`, and stamp identity. TimeoutError/ProviderError propagate to the F11 handlers
    (504/503)."""
    answer_text = ""
    meta = None
    async with aclosing(gen):
        async with asyncio.timeout(settings.REQUEST_TIMEOUT_S):
            async for ev in gen:
                if ev.event == "token":
                    answer_text += ev.data["token"]
                elif ev.event == "meta":
                    meta = ev.data
                elif ev.event == "error":
                    raise ProviderError(ev.data.get("message", "pipeline error"))
    if meta is None:
        raise ProviderError("pipeline ended without a meta event")
    resp = AnswerResponse(answer=answer_text, **meta)
    resp.request_id = request_id_var.get()
    resp.latency_ms = int((time.monotonic() - started) * 1000)
    return resp


@router.post("/ask", summary="Ask a question (SSE stream or JSON)",
             description="Streams stage/token/citations/meta/done events by default; returns one "
                         "AnswerResponse when called with Accept: application/json.",
             dependencies=[Depends(rate_limit_dep)])
async def ask(
    req: AskRequest,
    request: Request,
    principal: Principal | None = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_session),
):
    started = time.monotonic()
    flags = _flags_for_request(req, principal)
    wants_json = "application/json" in request.headers.get("accept", "")

    if not settings.ENABLE_MEMORY or req.session_id is None:
        gen = _stateless_events(req.question, req, flags, db)
    else:
        session = await _resolve_owned(request, req.session_id, principal, db)
        # per-session serialisation (F17 AC-31). Check + acquire is atomic on asyncio.Lock's
        # free-path acquire (it does not suspend), so no coroutine runs between them.
        lock = service.lock_for(session.id)
        if lock.locked():
            raise HTTPException(status.HTTP_409_CONFLICT, detail="session_busy")
        await lock.acquire()
        gen = _memory_events(req.question, req, flags, session, db, lock)

    if wants_json:
        return await _collect(gen, started)
    return StreamingResponse(_sse_stream(gen, started), media_type="text/event-stream")


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
