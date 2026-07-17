"""T4/T10 — the Redis fixed-window limiter (AC-8/9/10/11/12).

Unit-level against `FakeRedis`, plus a route-level check that the anon tier 429s and fails open.
"""

import pytest

from app.api import ask as ask_router
from app.auth import deps as auth_deps
from app.core import ratelimit
from app.core.errors import RateLimited
from tests.api.conftest import FakeRedis, Recorder, make_fake_astream, make_settings


async def test_over_limit_raises_with_retry_after():
    r = FakeRedis()
    for _ in range(3):
        await ratelimit.check("ip:1", 3, redis=r, window_s=60)  # 1,2,3 ok
    with pytest.raises(RateLimited) as exc:
        await ratelimit.check("ip:1", 3, redis=r, window_s=60)  # 4th over
    assert exc.value.retry_after > 0


async def test_separate_buckets_independent():
    r = FakeRedis()
    await ratelimit.check("ip:1", 1, redis=r, window_s=60)
    await ratelimit.check("ip:2", 1, redis=r, window_s=60)  # different bucket, still ok


async def test_window_id_in_key_shared_across_callers():
    """Two 'workers' sharing one FakeRedis see one shared count (the Redis guarantee, AC-10)."""
    shared = FakeRedis()
    await ratelimit.check("ip:1", 2, redis=shared, window_s=60)
    await ratelimit.check("ip:1", 2, redis=shared, window_s=60)
    with pytest.raises(RateLimited):
        await ratelimit.check("ip:1", 2, redis=shared, window_s=60)


async def test_fail_open_on_redis_error():
    r = FakeRedis(fail=True)
    # never raises even far past any limit
    for _ in range(100):
        await ratelimit.check("ip:1", 1, redis=r, window_s=60)


async def test_route_429_for_anon_tier(client, monkeypatch):
    rec = Recorder()
    hot = make_settings(ENABLE_RATE_LIMIT=True, RATE_LIMIT_ANON_PER_MIN=2)
    monkeypatch.setattr(ratelimit, "settings", hot)
    monkeypatch.setattr(auth_deps, "settings", hot)  # rate_tier reads limits from here
    monkeypatch.setattr(ask_router, "astream", make_fake_astream(rec))
    fake = FakeRedis()
    monkeypatch.setattr("app.caching.redis_hot._client", lambda s: fake)

    ok1 = await client.post("/api/ask", json={"question": "valid question"})
    ok2 = await client.post("/api/ask", json={"question": "valid question"})
    limited = await client.post("/api/ask", json={"question": "valid question"})
    assert ok1.status_code == ok2.status_code == 200
    assert limited.status_code == 429
    assert "retry-after" in {k.lower() for k in limited.headers}
    assert limited.json()["error"]["type"] == "rate_limited"


async def test_toggle_off_never_limits(client, monkeypatch):
    rec = Recorder()
    hot = make_settings(ENABLE_RATE_LIMIT=False, RATE_LIMIT_ANON_PER_MIN=1)
    monkeypatch.setattr(ratelimit, "settings", hot)
    monkeypatch.setattr(ask_router, "astream", make_fake_astream(rec))
    fake = FakeRedis()
    monkeypatch.setattr("app.caching.redis_hot._client", lambda s: fake)
    for _ in range(5):
        r = await client.post("/api/ask", json={"question": "valid question"})
        assert r.status_code == 200
