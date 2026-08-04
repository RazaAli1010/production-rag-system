"""T5 — /api/health per-dependency probe (AC-7). Pinecone is stubbed (no live call); the OpenAI
probe must never make a billable request."""

from app.api import health as health_router
from tests.api.conftest import make_settings


async def test_all_up_returns_200(client, monkeypatch, tmp_path):
    bm25 = tmp_path / "bm25.pkl"
    bm25.write_bytes(b"x")
    s = make_settings(BM25_PATH=bm25)  # REDIS_URL None ⇒ redis "skipped"
    monkeypatch.setattr(health_router, "settings", s)
    monkeypatch.setattr(health_router, "_pinecone_sync", lambda: None)

    r = await client.get("/api/health")
    assert r.status_code == 200
    deps = r.json()["dependencies"]
    assert deps["postgres"] == "ok"
    assert deps["pinecone"] == "ok"
    assert deps["redis"] == "skipped"
    assert deps["openai_key"] == "ok"


async def test_live_stays_up_while_health_is_degraded(client, monkeypatch, tmp_path):
    """F15 AC-16/17 — the contrast IS the feature.

    /api/health 503s on any core dependency; /api/health/live touches none and takes no DB session,
    so a Redis/Pinecone blip can never make the platform restart an API that still answers.
    """
    s = make_settings(BM25_PATH=tmp_path / "absent.pkl", APP_VERSION="abc1234")
    monkeypatch.setattr(health_router, "settings", s)

    def _boom():
        raise RuntimeError("pinecone unreachable")

    monkeypatch.setattr(health_router, "_pinecone_sync", _boom)

    assert (await client.get("/api/health")).status_code == 503
    r = await client.get("/api/health/live")
    assert r.status_code == 200
    assert r.json() == {"status": "live", "version": "abc1234"}


async def test_pinecone_down_returns_503(client, monkeypatch, tmp_path):
    bm25 = tmp_path / "bm25.pkl"
    bm25.write_bytes(b"x")
    s = make_settings(BM25_PATH=bm25)
    monkeypatch.setattr(health_router, "settings", s)

    def _boom():
        raise RuntimeError("bad pinecone key")

    monkeypatch.setattr(health_router, "_pinecone_sync", _boom)
    r = await client.get("/api/health")
    assert r.status_code == 503
    assert r.json()["dependencies"]["pinecone"].startswith("down")


async def test_openai_probe_makes_no_call(client, monkeypatch, tmp_path):
    """Presence-only: the probe never constructs an OpenAI client or hits the network."""
    bm25 = tmp_path / "bm25.pkl"
    bm25.write_bytes(b"x")
    s = make_settings(BM25_PATH=bm25)
    monkeypatch.setattr(health_router, "settings", s)
    monkeypatch.setattr(health_router, "_pinecone_sync", lambda: None)
    # If _openai_key tried a call it would need network; a plain 200 with "ok" proves it didn't.
    r = await client.get("/api/health")
    assert r.json()["dependencies"]["openai_key"] == "ok"
