"""T10/T11 — the /api/ask memory route: stages, ownership, 409, disconnect, summariser fallback,
and the end-to-end sliding-window / lazy-batch / over-budget behaviour.

`astream` (the F3 RAG pipeline) is faked throughout: F17 owns memory assembly + persistence + the
summarizing_memory stage, NOT retrieval. The fake records the `memory`/`session_id` the pipeline
received, which is how the window-content assertions are made end-to-end.
"""

import datetime as dt
import json
import uuid

import pytest

import app.db.engine as db_engine
from app.api import ask
from app.core.contracts import AnswerResponse, Citation, PipelineFlags
from app.db.enums import MessageRole
from app.db.models.chat import Message, Session
from app.memory import service
from app.rag.events import SSEEvent

_BASE = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)


# --------------------------------------------------------------------------- fakes & helpers
class Recorder:
    def __init__(self):
        self.memory = None
        self.session_id = None


def make_fake_astream(recorder: Recorder, answer_text="Grounded answer [1]"):
    async def _fake(question, *, k=5, namespace=None, flags=None, memory=None, session=None,
                    settings=None, sessionmaker=None, session_id=None):
        recorder.memory = memory
        recorder.session_id = session_id
        yield SSEEvent(event="stage", data={"stage": "searching", "status": "started", "ms": None})
        yield SSEEvent(event="stage", data={"stage": "searching", "status": "done", "ms": 1})
        for tok in answer_text.split(" "):
            yield SSEEvent(event="token", data={"token": tok + " "})
        cits = [Citation(chunk_id="d:0", doc_id="d", title="PU", url="http://x", quote="q")]
        resp = AnswerResponse(answer="", citations=cits, pipeline_flags=PipelineFlags(memory=True),
                              session_id=session_id, tokens_in=10, tokens_out=5)
        yield SSEEvent(event="citations", data={"citations": [c.model_dump() for c in cits]})
        yield SSEEvent(event="meta", data=resp.model_dump(exclude={"answer"}))
        yield SSEEvent(event="done", data={})

    return _fake


def parse_sse(text: str):
    events = []
    for block in text.strip().split("\n\n"):
        if not block.strip():
            continue
        ev = data = None
        for line in block.splitlines():
            if line.startswith("event: "):
                ev = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        events.append((ev, data))
    return events


async def _create_session(client, authed):
    r = await client.post("/api/sessions", headers=authed["headers"])
    return uuid.UUID(r.json()["id"])


async def _seed_pairs(session_id, n, tokens=3):
    async with db_engine.get_sessionmaker()() as db:
        for i in range(n):
            db.add(Message(session_id=session_id, role=MessageRole.user, content=f"q{i}",
                          token_count=tokens, created_at=_BASE + dt.timedelta(seconds=2 * i)))
            db.add(Message(session_id=session_id, role=MessageRole.assistant, content=f"a{i}",
                          token_count=tokens, created_at=_BASE + dt.timedelta(seconds=2 * i + 1)))
        await db.commit()


# --------------------------------------------------------------------------- tests
async def test_happy_path_stages_and_session_id(client, authed, monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(ask, "astream", make_fake_astream(rec))
    sid = await _create_session(client, authed)

    r = await client.post("/api/ask", json={"question": "BS deadline?", "session_id": str(sid)},
                          headers=authed["headers"])
    assert r.status_code == 200
    events = parse_sse(r.text)
    kinds = [e[0] for e in events]

    # summarizing_memory leads (skipped on the first turn — no pairs have slid out yet), before token
    first_stage = next(d for e, d in events if e == "stage")
    assert first_stage["stage"] == "summarizing_memory" and first_stage["status"] == "skipped"
    assert kinds.index("stage") < kinds.index("token")

    meta = next(d for e, d in events if e == "meta")
    assert meta["session_id"] == str(sid)  # surfaced (AC-16)
    assert rec.session_id == str(sid)

    await service.drain_writes()
    async with db_engine.get_sessionmaker()() as db:
        msgs = await service.get_messages(db, sid)
    assert [m.role for m in msgs] == [MessageRole.user, MessageRole.assistant]  # write-behind persisted
    assert msgs[1].content.startswith("Grounded answer")


async def test_concurrent_ask_returns_409(client, authed, monkeypatch):
    monkeypatch.setattr(ask, "astream", make_fake_astream(Recorder()))
    sid = await _create_session(client, authed)

    # hold the session's lock — the endpoint checks this exact process-global registry (AC-31)
    lock = service.lock_for(sid)
    await lock.acquire()
    try:
        r = await client.post("/api/ask", json={"question": "q", "session_id": str(sid)},
                              headers=authed["headers"])
        assert r.status_code == 409
        assert r.json()["detail"] == "session_busy"
    finally:
        lock.release()


async def test_missing_session_is_404(client, authed, monkeypatch):
    monkeypatch.setattr(ask, "astream", make_fake_astream(Recorder()))
    r = await client.post("/api/ask", json={"question": "q", "session_id": str(uuid.uuid4())},
                          headers=authed["headers"])
    assert r.status_code == 404


async def test_disconnect_persists_no_assistant(sessionmaker_, memory_settings, monkeypatch):
    """Drive the generator directly and aclose() before `done` — the exact shape of a client
    dropping mid-stream. The finally must release the lock and no assistant may persist (AC-12)."""
    monkeypatch.setattr(ask, "settings", memory_settings)

    async def _fake(question, **kw):
        yield SSEEvent(event="token", data={"token": "partial"})
        yield SSEEvent(event="done", data={})  # never reached — we aclose first

    monkeypatch.setattr(ask, "astream", _fake)

    async with sessionmaker_() as db:
        s = await service.create_session(db, user_id=None)
        await db.commit()
        lock = service.lock_for(s.id)
        await lock.acquire()
        gen = ask._memory_events("q", s, db, lock)
        first = await gen.__anext__()
        assert first.event in ("stage", "token")
        await gen.aclose()  # disconnect before done
        assert not lock.locked()  # finally released it

    await service.drain_writes()
    async with sessionmaker_() as db:
        msgs = await service.get_messages(db, s.id)
    assert all(m.role != MessageRole.assistant for m in msgs)  # no partial assistant (AC-12)


async def test_summarizer_failure_still_answers(client, authed, monkeypatch):
    """8 completed pairs → 3 pending → summariser is due; force it to raise. The turn must still
    answer (done present) and the summarizing_memory stage still closes (AC-27)."""
    rec = Recorder()
    monkeypatch.setattr(ask, "astream", make_fake_astream(rec))

    async def _boom(*a, **k):
        raise RuntimeError("provider 500")

    monkeypatch.setattr(ask.summarizer, "extend_summary", _boom)

    sid = await _create_session(client, authed)
    await _seed_pairs(sid, 8)

    r = await client.post("/api/ask", json={"question": "q9", "session_id": str(sid)},
                          headers=authed["headers"])
    events = parse_sse(r.text)
    kinds = [e[0] for e in events]
    assert ("done" in kinds)  # answered despite summariser failure
    mem_stage = [d for e, d in events if e == "stage" and d["stage"] == "summarizing_memory"]
    assert {s["status"] for s in mem_stage} == {"started", "done"}  # opened and closed


async def test_sliding_window_turn9_last5_pairs(client, authed, monkeypatch):
    """Seed 8 pairs; the 9th ask must hand the pipeline exactly the last 5 pairs, none older
    (AC-18/19). Under budget → no over-budget shrink."""
    rec = Recorder()
    monkeypatch.setattr(ask, "astream", make_fake_astream(rec))
    # make summariser deterministic (it IS due at 8 pairs) so the run doesn't hit OpenAI
    async def _fake_summary(old, pending, settings):
        return "rolling summary of q0..q2"

    monkeypatch.setattr(ask.summarizer, "extend_summary", _fake_summary)

    sid = await _create_session(client, authed)
    await _seed_pairs(sid, 8)

    await client.post("/api/ask", json={"question": "q9", "session_id": str(sid)},
                      headers=authed["headers"])

    contents = [m.content for m in rec.memory.pairs]
    assert contents == ["q3", "a3", "q4", "a4", "q5", "a5", "q6", "a6", "q7", "a7"]
    assert "q0" not in contents  # older pairs live only in the summary
    assert rec.memory.summary == "rolling summary of q0..q2"
    assert rec.memory.window_pairs == 5 and rec.memory.summarized is False


async def test_over_budget_shrinks_to_last2(client, authed, monkeypatch):
    """A session already over the 50k budget → the next ask hands the pipeline summary + exactly the
    last 2 pairs, window_pairs==2, summarized==True (AC-20)."""
    rec = Recorder()
    monkeypatch.setattr(ask, "astream", make_fake_astream(rec))

    async def _fake_summary(old, pending, settings):
        return "over-budget summary"

    monkeypatch.setattr(ask.summarizer, "extend_summary", _fake_summary)

    sid = await _create_session(client, authed)
    await _seed_pairs(sid, 8)
    # push the session over budget
    async with db_engine.get_sessionmaker()() as db:
        s = await db.get(Session, sid)
        s.total_tokens = 60_000
        await db.commit()

    await client.post("/api/ask", json={"question": "q9", "session_id": str(sid)},
                      headers=authed["headers"])

    contents = [m.content for m in rec.memory.pairs]
    assert contents == ["q6", "a6", "q7", "a7"]  # last 2 pairs only
    assert rec.memory.window_pairs == 2 and rec.memory.summarized is True
    assert rec.memory.effective_tokens < memory_settings_budget()


def memory_settings_budget():
    from .conftest import make_settings
    return make_settings().MEMORY_TOKEN_BUDGET
