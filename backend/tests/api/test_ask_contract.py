"""T7 — /api/ask input contract + effective-flags construction (AC-1/3/4).

The critical F11 logic: without `_flags_for_request`, `apply_flags` forces every enhancement off. So
these assert that deployed `ENABLE_*` actually reach the pipeline, that `skip_cache`/`deep`/admin
`flags_override` are honoured, and that bad input is rejected before any pipeline call.
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


async def test_flags_override_requires_admin(client, monkeypatch, student):
    rec = Recorder()
    monkeypatch.setattr(ask_router, "astream", make_fake_astream(rec))
    # anonymous / student → 403
    anon = await _ask(client, {"question": "valid question", "flags_override": {"hybrid": True}})
    assert anon.status_code == 403
    stud = await _ask(client, {"question": "valid question", "flags_override": {"hybrid": True}},
                      headers=student["headers"])
    assert stud.status_code == 403


async def test_flags_override_admin_applied(client, monkeypatch, admin):
    rec = Recorder()
    hot = make_settings(ENABLE_HYBRID=False)
    monkeypatch.setattr(ask_router, "settings", hot)
    monkeypatch.setattr(ask_router, "astream", make_fake_astream(rec))
    r = await _ask(client, {"question": "valid question", "flags_override": {"hybrid": True}},
                   headers=admin["headers"])
    assert r.status_code == 200
    assert rec.flags.hybrid is True


async def test_flags_override_unknown_key_422(client, monkeypatch, admin):
    rec = Recorder()
    monkeypatch.setattr(ask_router, "astream", make_fake_astream(rec))
    r = await _ask(client, {"question": "valid question", "flags_override": {"nope": True}},
                   headers=admin["headers"])
    assert r.status_code == 422
