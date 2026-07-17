"""T8 — content negotiation + request_id/latency stamping (AC-2/14)."""

from app.api import ask as ask_router
from tests.api.conftest import Recorder, make_fake_astream, parse_sse


async def test_json_variant_returns_answer_response(client, monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(ask_router, "astream", make_fake_astream(rec, answer="Hello world [1]"))
    r = await client.post("/api/ask", json={"question": "valid question"},
                          headers={"Accept": "application/json"})
    assert r.status_code == 200
    body = r.json()
    assert body["answer"] == "Hello world [1] "  # reassembled from token events
    assert body["citations"] and body["request_id"]
    assert body["request_id"] == r.headers["x-request-id"]
    assert isinstance(body["latency_ms"], int)


async def test_sse_is_default(client, monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(ask_router, "astream", make_fake_astream(rec))
    r = await client.post("/api/ask", json={"question": "valid question"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(r.text)
    kinds = [e for e, _ in events]
    assert "token" in kinds and ("meta" in kinds) and kinds[-1] == "done"
    meta = next(d for e, d in events if e == "meta")
    assert meta["request_id"] == r.headers["x-request-id"]
    assert isinstance(meta["latency_ms"], int)
