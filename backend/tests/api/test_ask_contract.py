"""T7 — /api/ask input contract + effective-flags construction (AC-1/3/4).

The critical F11 logic: without `_flags_for_request`, `apply_flags` forces every enhancement off. So
these assert that deployed `ENABLE_*` actually reach the pipeline, that `skip_cache`/`deep`/the
caller's `flags_override` are honoured, and that bad input is rejected before any pipeline call.
"""

import pytest

from app.api import ask as ask_router
from tests.api.conftest import Recorder, make_fake_astream, make_settings


async def _ask(client, body, headers=None):
    return await client.post("/api/ask", json=body, headers=headers or {})


@pytest.mark.parametrize("q", ["ab", "x" * 501])
async def test_question_bounds_rejected(client, monkeypatch, q):
    rec = Recorder()
    called = False

    def _guard(*a, **k):
        nonlocal called
        called = True
        return make_fake_astream(rec)(*a, **k)

    monkeypatch.setattr(ask_router, "astream", _guard)
    r = await _ask(client, {"question": q})
    assert r.status_code == 422
    assert r.json()["error"]["type"] == "validation_error"
    assert called is False  # rejected before the pipeline


async def test_bad_namespace_rejected(client):
    r = await _ask(client, {"question": "valid question", "namespace": "xx"})
    assert r.status_code == 422


async def test_flags_built_from_settings(client, monkeypatch, sessionmaker_):
    """ENABLE_HYBRID/RERANK on in Settings must reach the pipeline as flags (the bug F11 fixes)."""
    rec = Recorder()
    hot = make_settings(ENABLE_HYBRID=True, ENABLE_RERANK=True, ENABLE_CACHE=True)
    monkeypatch.setattr(ask_router, "settings", hot)
    monkeypatch.setattr(ask_router, "astream", make_fake_astream(rec))
    r = await _ask(client, {"question": "valid question"})
    assert r.status_code == 200
    assert rec.flags.hybrid is True and rec.flags.rerank is True
    assert rec.flags.cache is True


async def test_skip_cache_disables_cache_flag(client, monkeypatch):
    rec = Recorder()
    hot = make_settings(ENABLE_CACHE=True)
    monkeypatch.setattr(ask_router, "settings", hot)
    monkeypatch.setattr(ask_router, "astream", make_fake_astream(rec))
    r = await _ask(client, {"question": "valid question", "skip_cache": True})
    assert r.status_code == 200
    assert rec.flags.cache is False


async def test_deep_selects_deep_model(client, monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(ask_router, "astream", make_fake_astream(rec))
    r = await _ask(client, {"question": "valid question", "deep": True})
    assert r.status_code == 200
    assert rec.model == "gpt-4o"


async def test_flags_override_open_to_every_caller(client, monkeypatch, student):
    """The F14 picker lets a user choose their own pipeline, so anonymous and student callers may
    override — flags gate cost, not privilege, and `rate_limit_dep` is the guard."""
    rec = Recorder()
    hot = make_settings(ENABLE_HYBRID=False, ENABLE_RERANK=True)
    monkeypatch.setattr(ask_router, "settings", hot)
    monkeypatch.setattr(ask_router, "astream", make_fake_astream(rec))

    anon = await _ask(client, {"question": "valid question",
                               "flags_override": {"hybrid": True, "rerank": False}})
    assert anon.status_code == 200
    assert rec.flags.hybrid is True and rec.flags.rerank is False

    stud = await _ask(client, {"question": "valid question", "flags_override": {"hybrid": True}},
                      headers=student["headers"])
    assert stud.status_code == 200
    assert rec.flags.hybrid is True


async def test_flags_override_echoed_on_response(client, monkeypatch):
    """What the user picked must be what `pipeline_flags` reports — the UI renders it."""
    rec = Recorder()
    monkeypatch.setattr(ask_router, "settings", make_settings(ENABLE_COMPRESSION=True))
    monkeypatch.setattr(ask_router, "astream", make_fake_astream(rec))
    r = await _ask(client, {"question": "valid question",
                            "flags_override": {"hybrid": True, "compression": False}},
                   headers={"Accept": "application/json"})
    assert r.status_code == 200
    assert r.json()["pipeline_flags"]["hybrid"] is True
    assert r.json()["pipeline_flags"]["compression"] is False


async def test_flags_override_unknown_key_422(client, monkeypatch, admin):
    rec = Recorder()
    monkeypatch.setattr(ask_router, "astream", make_fake_astream(rec))
    r = await _ask(client, {"question": "valid question", "flags_override": {"nope": True}},
                   headers=admin["headers"])
    assert r.status_code == 422


async def test_memory_override_off_runs_stateless(client, monkeypatch, sessionmaker_, student):
    """Regression: the route used to branch on the global ENABLE_MEMORY, so `memory: false` still
    ran the stateful turn while `pipeline_flags` claimed otherwise. No messages may be written."""
    from sqlalchemy import func, select

    from app.db.models.chat import Message

    rec = Recorder()
    monkeypatch.setattr(ask_router, "settings", make_settings(ENABLE_MEMORY=True))
    monkeypatch.setattr(ask_router, "astream", make_fake_astream(rec))

    created = await client.post("/api/sessions", headers=student["headers"])
    assert created.status_code in (200, 201)
    sid = created.json()["id"]

    r = await _ask(client, {"question": "valid question", "session_id": sid,
                            "flags_override": {"memory": False}}, headers=student["headers"])
    assert r.status_code == 200
    assert rec.flags.memory is False
    assert rec.session_id is None  # stateless path never binds the session

    from app.memory import service
    await service.drain_writes()
    async with sessionmaker_() as db:
        n = await db.scalar(select(func.count()).select_from(Message)
                            .where(Message.session_id == sid))
    assert n == 0
