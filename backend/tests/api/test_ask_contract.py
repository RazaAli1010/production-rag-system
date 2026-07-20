"""T7 — /api/ask input contract + effective-flags construction (AC-1/3/4).

The critical F11 logic: without `_flags_for_request`, `apply_flags` forces every enhancement off. So
these assert that deployed `ENABLE_*` actually reach the pipeline, that `skip_cache`/`deep` are
honoured, and that bad input is rejected before any pipeline call. The per-request `flags_override`
is gone — see `test_flags_override_ignored`.
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


async def test_settings_flags_echoed_on_response(client, monkeypatch):
    """`pipeline_flags` reports what actually ran — the UI renders it, and it is now the only way
    to see which stages were active."""
    rec = Recorder()
    monkeypatch.setattr(ask_router, "settings",
                        make_settings(ENABLE_HYBRID=True, ENABLE_COMPRESSION=False))
    monkeypatch.setattr(ask_router, "astream", make_fake_astream(rec))
    r = await _ask(client, {"question": "valid question"}, headers={"Accept": "application/json"})
    assert r.status_code == 200
    assert r.json()["pipeline_flags"]["hybrid"] is True
    assert r.json()["pipeline_flags"]["compression"] is False


async def test_flags_override_ignored(client, monkeypatch):
    """The per-request override is GONE: it applied last, so the frontend's all-false defaults
    silently pinned every browser ask to the bare F3 baseline regardless of the deployed config.

    A stale cached bundle may still post the field — it must be ignored, not 422, so an old tab
    keeps working rather than breaking on every ask."""
    rec = Recorder()
    monkeypatch.setattr(ask_router, "settings", make_settings(ENABLE_HYBRID=True))
    monkeypatch.setattr(ask_router, "astream", make_fake_astream(rec))
    r = await _ask(client, {"question": "valid question",
                            "flags_override": {"hybrid": False, "nope": True}})
    assert r.status_code == 200
    assert rec.flags.hybrid is True  # settings won; the override did nothing


async def test_memory_off_in_settings_runs_stateless(client, monkeypatch, sessionmaker_, student):
    """`flags.memory` off must run the stateless path even when a session_id is supplied — the
    route branches on the flag, not on the id. No messages may be written."""
    from sqlalchemy import func, select

    from app.db.models.chat import Message

    rec = Recorder()
    monkeypatch.setattr(ask_router, "settings", make_settings(ENABLE_MEMORY=False))
    monkeypatch.setattr(ask_router, "astream", make_fake_astream(rec))

    created = await client.post("/api/sessions", headers=student["headers"])
    assert created.status_code in (200, 201)
    sid = created.json()["id"]

    r = await _ask(client, {"question": "valid question", "session_id": sid},
                   headers=student["headers"])
    assert r.status_code == 200
    assert rec.flags.memory is False
    assert rec.session_id is None  # stateless path never binds the session

    from app.memory import service
    await service.drain_writes()
    async with sessionmaker_() as db:
        n = await db.scalar(select(func.count()).select_from(Message)
                            .where(Message.session_id == sid))
    assert n == 0
