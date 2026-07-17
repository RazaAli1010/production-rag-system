"""Async DB surface for F17 sessions/messages + the per-session lock registry (design §3.3).

Everything here is async SQLAlchemy (AC-32). Two correctness points worth stating up front:

- **`total_tokens` uses atomic SQL increments, never ORM `+=`.** A turn's user message is written on
  the request session while the PREVIOUS turn's assistant message may still be landing on its own
  write-behind session (the per-session lock is released when the ask generator ends, before the
  write-behind task runs). Two `session.total_tokens += n` on stale copies would lose an update; an
  `UPDATE ... SET total_tokens = total_tokens + n` serialises on the row lock and is exact (AC-13).
- **The summariser trigger is derived, not stored.** No `needs_summarize`/`pending_pairs` column
  exists (design §2). Pending-pair count = completed pairs beyond the verbatim window, minus the
  pairs already folded (tracked by `summarized_upto_message_id`). One cheap count query on the
  amortised summary path; the window CONTENT load stays a single indexed query (AC-22).
"""

import asyncio
import uuid

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.orm.attributes import set_committed_value

from app.core.contracts import AnswerResponse, MemoryContext
from app.db.enums import MessageRole
from app.db.models.chat import Message, Session
from app.memory import tokens, window

logger = structlog.get_logger(__name__)

# --------------------------------------------------------------------------- per-session lock
# Process-local. One API replica assumed (design §"Risks"): a second replica would let two nodes
# run the same session concurrently — same posture as F9's single-process cache matrix.
# ponytail: in-process registry; add distributed locking only if the API ever runs >1 replica.
_LOCKS: dict[uuid.UUID, asyncio.Lock] = {}


def lock_for(session_id: uuid.UUID) -> asyncio.Lock:
    return _LOCKS.setdefault(session_id, asyncio.Lock())


def reset_locks() -> None:
    """Test hook — locks are loop-bound, so the suite drops them between tests."""
    _LOCKS.clear()


# --------------------------------------------------------------------------- write-behind refs
# asyncio holds only a WEAK ref to a running task; without a strong ref the assistant write can be
# GC'd mid-await and silently never land (the canonical create_task footgun — mirrors
# caching.store._WRITE_TASKS).
_WRITE_TASKS: set[asyncio.Task] = set()


# --------------------------------------------------------------------------- session CRUD
async def create_session(db, *, user_id: uuid.UUID | None) -> Session:
    s = Session(user_id=user_id)
    db.add(s)
    await db.flush()  # assign id
    return s


async def list_sessions(db, *, user_id: uuid.UUID) -> list[Session]:
    rows = await db.execute(
        select(Session)
        .where(Session.user_id == user_id, Session.is_archived.is_(False))
        .order_by(Session.last_active_at.desc())
    )
    return list(rows.scalars().all())


async def get_owned(db, session_id: uuid.UUID, *, user_id: uuid.UUID | None) -> Session | None:
    """Returns the session iff the caller owns it and it is live, else `None` (the router maps that
    to 404 so existence is not an enumeration oracle, AC-6). `user_id=None` is an anonymous caller;
    ownership of an anon session is proven by the signed cookie the router already verified, so here
    we only refuse a *user-bound* session to an anon caller."""
    s = await db.get(Session, session_id)
    if s is None or s.is_archived:
        return None
    if user_id is not None:
        return s if s.user_id == user_id else None
    return s if s.user_id is None else None


async def get_messages(db, session_id: uuid.UUID) -> list[Message]:
    """FULL transcript in created_at order (AC-4) — the window limits only the LLM prompt."""
    rows = await db.execute(
        select(Message).where(Message.session_id == session_id).order_by(Message.created_at)
    )
    return list(rows.scalars().all())


async def archive(db, session_id: uuid.UUID) -> None:
    await db.execute(
        update(Session).where(Session.id == session_id).values(is_archived=True)
        .execution_options(synchronize_session=False)
    )


async def count_messages(db, session_id: uuid.UUID) -> int:
    return await db.scalar(
        select(func.count()).select_from(Message).where(Message.session_id == session_id)
    )


# --------------------------------------------------------------------------- writes
async def persist_user(db, session: Session, question: str, settings) -> Message:
    """Write the user question BEFORE the pipeline (AC-10) so its created_at sorts first. Sets the
    auto-title if unset (AC-2) and increments total_tokens atomically (AC-13).

    The increment is a Core UPDATE with `synchronize_session=False` (atomic at the row, so a
    concurrent assistant write-behind can't lose an update), then `set_committed_value` mirrors the
    new total_tokens/title onto the in-memory `session` WITHOUT marking it dirty — so the ORM never
    re-issues (and never overwrites) the atomic value, and no `refresh` is needed. A plain refresh
    here raised "not persistent within this Session"; a plain attribute assignment would dirty the
    row and re-write it non-atomically."""
    tok = tokens.count(question)
    title = question[: settings.MEMORY_SESSION_TITLE_MAX_CHARS]
    msg = Message(session_id=session.id, role=MessageRole.user, content=question, token_count=tok)
    db.add(msg)
    await db.flush()  # assign id + created_at
    await db.execute(
        update(Session)
        .where(Session.id == session.id)
        .values(
            total_tokens=Session.total_tokens + tok,
            # coalesce → title is set only when currently NULL (first question)
            title=func.coalesce(Session.title, title),
            last_active_at=func.now(),
        )
        .execution_options(synchronize_session=False)
    )
    set_committed_value(session, "total_tokens", (session.total_tokens or 0) + tok)
    if session.title is None:
        set_committed_value(session, "title", title)
    return msg


async def apply_summary(db, session: Session, new_summary: str, *, upto_id: uuid.UUID) -> None:
    """Persist the extended rolling summary and advance the fold pointer (AC-23). `upto_id` is the
    last (assistant) message folded in."""
    session.summary = new_summary
    session.summary_token_count = tokens.count(new_summary)
    session.summarized_upto_message_id = upto_id
    await db.flush()


async def _persist_assistant_guarded(session_id, response: AnswerResponse, *, sessionmaker) -> None:
    """Write-behind body. Opens its OWN short-lived session (it outlives the request session, which
    closes when the SSE stream ends) and swallows every error — a failed transcript write must never
    surface to the client (mirrors caching.store._write_guarded)."""
    try:
        async with sessionmaker() as db:
            tok = tokens.count(response.answer)
            db.add(
                Message(
                    session_id=session_id,
                    role=MessageRole.assistant,
                    content=response.answer,
                    token_count=tok,
                    citations=[c.model_dump() for c in response.citations] or None,
                    refused=response.refused,
                )
            )
            await db.execute(
                update(Session)
                .where(Session.id == session_id)
                .values(total_tokens=Session.total_tokens + tok, last_active_at=func.now())
                .execution_options(synchronize_session=False)
            )
            await db.commit()
    except Exception as exc:  # noqa: BLE001 — a failed transcript write is never fatal
        logger.warning("memory.persist_failed", session_id=str(session_id), error=str(exc))


def schedule_persist_assistant(
    session_id, response: AnswerResponse, *, sessionmaker
) -> asyncio.Task:
    """Fire-and-forget the assistant write AFTER the terminal `done` (AC-11). Returns the task so
    tests can drain it; callers ignore it."""
    task = asyncio.create_task(
        _persist_assistant_guarded(session_id, response, sessionmaker=sessionmaker)
    )
    _WRITE_TASKS.add(task)
    task.add_done_callback(_WRITE_TASKS.discard)
    return task


async def drain_writes() -> None:
    """Await every in-flight write-behind task. For tests and F11's shutdown hook — never the
    request path."""
    if _WRITE_TASKS:
        await asyncio.gather(*list(_WRITE_TASKS), return_exceptions=True)


# --------------------------------------------------------------------------- memory load
async def _completed_pairs(db, session_id: uuid.UUID) -> int:
    """A completed pair has exactly one assistant message, so the assistant count IS the pair count
    (the current trailing question is a lone user message, uncounted)."""
    return await db.scalar(
        select(func.count())
        .select_from(Message)
        .where(Message.session_id == session_id, Message.role == MessageRole.assistant)
    )


async def _summarized_pairs(db, session: Session) -> int:
    if session.summarized_upto_message_id is None:
        return 0
    boundary = (
        select(Message.created_at)
        .where(Message.id == session.summarized_upto_message_id)
        .scalar_subquery()
    )
    return await db.scalar(
        select(func.count())
        .select_from(Message)
        .where(
            Message.session_id == session.id,
            Message.role == MessageRole.assistant,
            Message.created_at <= boundary,
        )
    )


async def _load_window(db, session_id: uuid.UUID, settings) -> list[Message]:
    """The last-`window` messages oldest→newest — the single indexed content query (AC-22). Loads
    `MEMORY_WINDOW_PAIRS*2 + 1` rows: the +1 catches the just-persisted current question, which the
    assembler drops as an unpaired trailing user message."""
    limit = settings.MEMORY_WINDOW_PAIRS * 2 + 1
    rows = await db.execute(
        select(Message)
        .where(Message.session_id == session_id)
        .order_by(Message.created_at.desc())
        .limit(limit)
    )
    return list(reversed(rows.scalars().all()))


async def _load_pending(db, session: Session, n_pairs: int) -> list[Message]:
    """The oldest `n_pairs` not-yet-summarised pairs (the slid-out batch to fold, AC-23/25). Loaded
    oldest-first from just past the summary boundary; since the window is the NEWEST pairs, the
    oldest unsummarised rows are exactly the slid-out ones."""
    q = select(Message).where(Message.session_id == session.id)
    if session.summarized_upto_message_id is not None:
        boundary = (
            select(Message.created_at)
            .where(Message.id == session.summarized_upto_message_id)
            .scalar_subquery()
        )
        q = q.where(Message.created_at > boundary)
    q = q.order_by(Message.created_at).limit(n_pairs * 2)
    rows = await db.execute(q)
    return list(rows.scalars().all())


async def load_memory(db, session: Session, settings) -> tuple[list[Message], list[Message] | None]:
    """Returns `(recent_window_messages, pending_or_None)`.

    `pending` is the slid-out, unsummarised pairs the caller must fold into the summary THIS turn
    (before retrieval) — `None` when no summariser call is due. The threshold is
    `MEMORY_SUMMARIZE_EVERY_PAIRS` normally, but `1` when over budget (fold immediately, AC-25).
    The window content itself is one indexed query; the trigger adds a cheap count.
    """
    recent = await _load_window(db, session.id, settings)

    over_budget = session.total_tokens >= settings.MEMORY_TOKEN_BUDGET
    window_pairs = settings.MEMORY_KEEP_LAST_PAIRS if over_budget else settings.MEMORY_WINDOW_PAIRS
    completed = await _completed_pairs(db, session.id)
    slid_out = max(0, completed - window_pairs)
    summarized = await _summarized_pairs(db, session)
    pending_pairs = max(0, slid_out - summarized)

    threshold = 1 if over_budget else settings.MEMORY_SUMMARIZE_EVERY_PAIRS
    if pending_pairs >= threshold:
        pending = await _load_pending(db, session, pending_pairs)
        return recent, (pending or None)
    return recent, None


def assemble(session: Session, recent: list[Message], settings) -> MemoryContext:
    """Thin re-export of the pure window rule, so callers touch one module (design §3.1)."""
    return window.assemble(session, recent, settings)
