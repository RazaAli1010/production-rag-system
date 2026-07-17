"""T5 — service DB ops, atomic token accounting, ownership, write-behind (live Postgres)."""

import datetime as dt
import uuid

from app.db.enums import MessageRole
from app.db.models.chat import Message
from app.db.models.user import User
from app.memory import service

_BASE = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)


async def _seed_user(session) -> uuid.UUID:
    u = User(email=f"{uuid.uuid4().hex}@pu.edu.pk", hashed_password="x")
    session.add(u)
    await session.flush()
    return u.id


async def _seed_session(session, user_id=None):
    s = await service.create_session(session, user_id=user_id)
    await session.commit()
    return s


async def _seed_pairs_from(session, session_id, start, n):
    """n completed pairs numbered q{start}..q{start+n-1}, with explicit strictly-increasing
    created_at — production writes each message in its own transaction (distinct func.now()); the
    test batches them, so it sets the timestamps that separation would otherwise produce."""
    for j in range(n):
        i = start + j
        session.add(Message(session_id=session_id, role=MessageRole.user, content=f"q{i}",
                            token_count=3, created_at=_BASE + dt.timedelta(seconds=2 * i)))
        session.add(Message(session_id=session_id, role=MessageRole.assistant, content=f"a{i}",
                            token_count=3, created_at=_BASE + dt.timedelta(seconds=2 * i + 1)))
    await session.flush()


async def _seed_pairs(session, session_id, n):
    await _seed_pairs_from(session, session_id, 0, n)


async def test_persist_user_writes_first_and_sets_title(session, memory_settings):
    s = await _seed_session(session)
    msg = await service.persist_user(session, s, "BS admission deadline?", memory_settings)
    await session.commit()

    assert msg.role == MessageRole.user
    assert s.title == "BS admission deadline?"  # auto-title from first question (AC-2)
    assert s.total_tokens == msg.token_count  # running sum (AC-13)


async def test_title_truncated_to_60(session, memory_settings):
    s = await _seed_session(session)
    long_q = "x" * 200
    await service.persist_user(session, s, long_q, memory_settings)
    await session.commit()
    assert len(s.title) == 60


async def test_total_tokens_is_atomic_sum_across_user_and_assistant(session, sessionmaker_, memory_settings):
    """User (request session) + assistant (write-behind session) increment concurrently — the sum
    must be exact, which is why the increments are atomic SQL, not ORM `+=` (AC-13)."""
    s = await _seed_session(session)
    umsg = await service.persist_user(session, s, "question one", memory_settings)
    await session.commit()

    from app.core.contracts import AnswerResponse, Citation, PipelineFlags

    resp = AnswerResponse(
        answer="the answer text [1]",
        citations=[Citation(chunk_id="d:0", doc_id="d", title="PU", url="http://x", quote="q")],
        pipeline_flags=PipelineFlags(memory=True),
    )
    task = service.schedule_persist_assistant(s.id, resp, sessionmaker=sessionmaker_)
    await task  # deterministic drain

    # reload the session row and assert the sum equals both messages
    async with sessionmaker_() as db:
        reloaded = await db.get(type(s), s.id)
        from app.memory import tokens

        expected = tokens.count("question one") + tokens.count("the answer text [1]")
        assert reloaded.total_tokens == expected
        msgs = await service.get_messages(db, s.id)
        assert [m.role for m in msgs] == [MessageRole.user, MessageRole.assistant]  # ordering (AC-10)
        assert msgs[0].created_at < msgs[1].created_at
        assert msgs[1].citations is not None  # citations serialized on assistant turn


async def test_get_owned_refuses_foreign_session(session, memory_settings):
    owner = await _seed_user(session)
    s = await _seed_session(session, user_id=owner)
    other = await _seed_user(session)
    assert await service.get_owned(session, s.id, user_id=owner) is not None
    assert await service.get_owned(session, s.id, user_id=other) is None  # foreign → None → 404
    assert await service.get_owned(session, uuid.uuid4(), user_id=owner) is None  # missing → None


async def test_get_owned_anon_cannot_read_user_session(session, memory_settings):
    owner = await _seed_user(session)
    s = await _seed_session(session, user_id=owner)
    assert await service.get_owned(session, s.id, user_id=None) is None  # anon can't take a bound session


async def test_archive_hides_from_list(session, memory_settings):
    owner = await _seed_user(session)
    s = await _seed_session(session, user_id=owner)
    await service.archive(session, s.id)
    await session.commit()
    assert await service.list_sessions(session, user_id=owner) == []


async def test_load_window_is_bounded_and_drops_current_question(session, memory_settings):
    """8 completed pairs + a trailing current question; the window load returns at most
    WINDOW_PAIRS*2+1 rows and the assembler keeps only the last 5 pairs (AC-22)."""
    s = await _seed_session(session)
    await _seed_pairs(session, s.id, 8)
    # current question (no assistant yet), newest
    session.add(Message(session_id=s.id, role=MessageRole.user, content="current",
                        token_count=3, created_at=_BASE + dt.timedelta(seconds=100)))
    await session.commit()

    recent, pending = await service.load_memory(session, s, memory_settings)
    assert len(recent) <= memory_settings.MEMORY_WINDOW_PAIRS * 2 + 1

    ctx = service.assemble(s, recent, memory_settings)
    contents = [m.content for m in ctx.pairs]
    assert contents == ["q3", "a3", "q4", "a4", "q5", "a5", "q6", "a6", "q7", "a7"]
    assert "current" not in contents


async def test_summariser_is_lazy_batched_one_call_per_three_pairs(session, memory_settings):
    """Under budget the summariser fires only once 3 pairs have slid past the 5-window, and the fold
    pointer advances so the same pairs are never re-summarised (AC-23/24)."""
    s = await _seed_session(session)

    # 5 pairs → nothing has slid out of the 5-window yet → not due
    await _seed_pairs(session, s.id, 5)
    await session.commit()
    _, pending = await service.load_memory(session, s, memory_settings)
    assert pending is None

    # 8 pairs → 3 slid out, none summarised → due, exactly the 3 oldest pairs (q0..q2)
    await _seed_pairs_from(session, s.id, 5, 3)
    await session.commit()
    _, pending = await service.load_memory(session, s, memory_settings)
    assert pending is not None
    assert [m.content for m in pending] == ["q0", "a0", "q1", "a1", "q2", "a2"]

    # fold them and advance the pointer; next load is NOT due again (0 new pending)
    await service.apply_summary(session, s, "summary of q0..q2", upto_id=pending[-1].id)
    await session.commit()
    _, pending = await service.load_memory(session, s, memory_settings)
    assert pending is None

    # 3 more pairs slide out (11 total) → due again — one call per 3 (AC-23)
    await _seed_pairs_from(session, s.id, 8, 3)
    await session.commit()
    _, pending = await service.load_memory(session, s, memory_settings)
    assert [m.content for m in pending] == ["q3", "a3", "q4", "a4", "q5", "a5"]
