"""T9 — server-side timeout (AC-17). A pipeline that runs past REQUEST_TIMEOUT_S becomes a terminal
SSE `error` (the 200 stream already started, so status can't change) or a 504 for the JSON variant.
Client-disconnect cancellation (AC-18) is inherited from F17's clean-`done` write-behind gate and
covered by tests/memory/test_ask_memory.py."""

from app.api import ask as ask_router
from tests.api.conftest import Recorder, make_fake_astream, make_settings, parse_sse


async def test_sse_timeout_emits_error_event(client, monkeypatch):
    rec = Recorder()
    fast_timeout = make_settings(REQUEST_TIMEOUT_S=0.05)
    monkeypatch.setattr(ask_router, "settings", fast_timeout)
    monkeypatch.setattr(ask_router, "astream", make_fake_astream(rec, delay=0.3))
    r = await client.post("/api/ask", json={"question": "valid question"})
    assert r.status_code == 200  # stream already opened
    events = parse_sse(r.text)
    assert events[-1][0] == "error"
    assert "timed out" in events[-1][1]["message"]


async def test_json_timeout_returns_504(client, monkeypatch):
    rec = Recorder()
    fast_timeout = make_settings(REQUEST_TIMEOUT_S=0.05)
    monkeypatch.setattr(ask_router, "settings", fast_timeout)
    monkeypatch.setattr(ask_router, "astream", make_fake_astream(rec, delay=0.3))
    r = await client.post("/api/ask", json={"question": "valid question"},
                          headers={"Accept": "application/json"})
    assert r.status_code == 504
    assert r.json()["error"]["type"] == "timeout"
