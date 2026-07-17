"""T12 — ENABLE_MEMORY=false OR no session_id ⇒ stateless single turn, byte-for-byte
f9-cache-after: no memory loaded, no summarizing_memory stage, nothing persisted (AC-33)."""

from app.api import ask
from app.memory import service

from .conftest import make_settings
from .test_ask_memory import Recorder, _create_session, make_fake_astream, parse_sse
import app.db.engine as db_engine


async def test_memory_off_ignores_session_id(client, authed, monkeypatch):
    monkeypatch.setattr(ask, "settings", make_settings(ENABLE_MEMORY=False))
    rec = Recorder()
    monkeypatch.setattr(ask, "astream", make_fake_astream(rec))

    sid = await _create_session(client, authed)
    r = await client.post("/api/ask", json={"question": "qqq", "session_id": str(sid)},
                          headers=authed["headers"])
    assert r.status_code == 200

    events = parse_sse(r.text)
    assert not any(d and d.get("stage") == "summarizing_memory" for e, d in events if e == "stage")
    assert rec.memory is None  # no MemoryContext passed to the pipeline

    await service.drain_writes()
    async with db_engine.get_sessionmaker()() as db:
        msgs = await service.get_messages(db, sid)
    assert msgs == []  # nothing persisted on the stateless path


async def test_missing_session_id_is_stateless(client, authed, monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(ask, "astream", make_fake_astream(rec))  # memory ON but no session_id

    r = await client.post("/api/ask", json={"question": "qqq"}, headers=authed["headers"])
    assert r.status_code == 200
    events = parse_sse(r.text)
    assert not any(d and d.get("stage") == "summarizing_memory" for e, d in events if e == "stage")
    assert rec.memory is None
