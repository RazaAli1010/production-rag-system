"""Redis hot tier. No real Redis anywhere — every test injects a fake client (F2's DI style), which
is what lets the `caching:` CI job run without a Redis service. The fail-open contract is the point
of this file: a cache outage must degrade latency, never availability (AC-3).
"""

import asyncio
import json

import pytest

from app.caching import redis_hot
from tests.cache.conftest import make_settings


class FakeRedis:
    def __init__(self, *, raises: Exception | None = None, hangs: bool = False):
        self.store: dict[str, str] = {}
        self.raises = raises
        self.hangs = hangs
        self.calls: list[str] = []

    async def _maybe_fail(self, op: str):
        self.calls.append(op)
        if self.hangs:
            await asyncio.sleep(10)
        if self.raises is not None:
            raise self.raises

    async def get(self, key):
        await self._maybe_fail("get")
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        await self._maybe_fail("set")
        self.store[key] = value
        return True

    async def delete(self, *keys):
        await self._maybe_fail("delete")
        return sum(1 for k in keys if self.store.pop(k, None) is not None)

    async def scan_iter(self, match=None):
        await self._maybe_fail("scan")
        prefix = (match or "").rstrip("*")
        for key in list(self.store):
            if key.startswith(prefix):
                yield key

    async def aclose(self):
        pass


@pytest.fixture(autouse=True)
def _clear_client_cache():
    redis_hot._CLIENTS.clear()
    yield
    redis_hot._CLIENTS.clear()


def _inject(monkeypatch, fake):
    monkeypatch.setattr(redis_hot, "_client", lambda settings: fake)


# --------------------------------------------------------------- REDIS_URL unset (AC-4)

async def test_redis_url_none_returns_miss_and_constructs_nothing(monkeypatch):
    """An unconfigured hot tier is a supported deployment, not a degradation: no client is built,
    and crucially no warning is logged (it would fire on every request forever)."""
    built = []
    monkeypatch.setattr(redis_hot.redis, "from_url", lambda *a, **k: built.append(1))
    settings = make_settings(REDIS_URL=None)

    assert await redis_hot.get("k", settings=settings) is None
    await redis_hot.set("k", {"a": 1}, settings=settings)
    assert await redis_hot.flush(settings=settings) == 0
    assert built == []


# --------------------------------------------------------------- happy path

async def test_round_trip(monkeypatch):
    fake = FakeRedis()
    _inject(monkeypatch, fake)
    settings = make_settings(REDIS_URL="redis://localhost:6379/0")

    await redis_hot.set("k1", {"answer": "hello", "cache_hit": True}, settings=settings)
    got = await redis_hot.get("k1", settings=settings)
    assert got == {"answer": "hello", "cache_hit": True}


async def test_set_applies_ttl(monkeypatch):
    captured = {}

    class TTLRedis(FakeRedis):
        async def set(self, key, value, ex=None):
            captured["ex"] = ex
            return await super().set(key, value, ex=ex)

    fake = TTLRedis()
    _inject(monkeypatch, fake)
    settings = make_settings(REDIS_URL="redis://localhost:6379/0", CACHE_REDIS_TTL_S=1234)

    await redis_hot.set("k", {"a": 1}, settings=settings)
    assert captured["ex"] == 1234


async def test_get_miss_returns_none(monkeypatch):
    _inject(monkeypatch, FakeRedis())
    settings = make_settings(REDIS_URL="redis://localhost:6379/0")
    assert await redis_hot.get("absent", settings=settings) is None


# --------------------------------------------------------------- fail-open (AC-3)

async def test_connection_error_degrades_to_miss_and_logs(monkeypatch):
    fake = FakeRedis(raises=ConnectionError("redis is down"))
    _inject(monkeypatch, fake)
    settings = make_settings(REDIS_URL="redis://localhost:6379/0")

    assert await redis_hot.get("k", settings=settings) is None
    await redis_hot.set("k", {"a": 1}, settings=settings)  # must not raise
    assert await redis_hot.flush(settings=settings) == 0  # must not raise


async def test_hanging_redis_times_out_rather_than_blocking_the_request(monkeypatch):
    """A hot tier slower than the miss it saves is worse than no hot tier."""
    _inject(monkeypatch, FakeRedis(hangs=True))
    settings = make_settings(REDIS_URL="redis://localhost:6379/0", CACHE_REDIS_TIMEOUT_S=0.05)

    started = asyncio.get_event_loop().time()
    assert await redis_hot.get("k", settings=settings) is None
    elapsed = asyncio.get_event_loop().time() - started
    assert elapsed < 1.0, "get() did not honour CACHE_REDIS_TIMEOUT_S"


async def test_corrupt_payload_is_dropped_not_served(monkeypatch):
    fake = FakeRedis()
    fake.store["k"] = "{not json"
    _inject(monkeypatch, fake)
    settings = make_settings(REDIS_URL="redis://localhost:6379/0")

    assert await redis_hot.get("k", settings=settings) is None
    assert "k" not in fake.store, "a corrupt entry must be evicted so the next request repopulates"


# --------------------------------------------------------------- flush (AC-20)

async def test_flush_deletes_only_prefixed_keys(monkeypatch):
    fake = FakeRedis()
    fake.store = {
        "campusrag:cache:aaa": json.dumps({"a": 1}),
        "campusrag:cache:bbb": json.dumps({"b": 2}),
        "campusrag:ratelimit:user1": "9",  # F11's future keys must survive a cache flush
        "unrelated": "x",
    }
    _inject(monkeypatch, fake)
    settings = make_settings(REDIS_URL="redis://localhost:6379/0")

    deleted = await redis_hot.flush(settings=settings)

    assert deleted == 2
    assert set(fake.store) == {"campusrag:ratelimit:user1", "unrelated"}


async def test_flush_uses_scan_never_keys(monkeypatch):
    """KEYS blocks the whole Redis server for the keyspace scan — unacceptable on a shared
    free-tier instance."""
    fake = FakeRedis()
    fake.store = {"campusrag:cache:a": json.dumps({})}
    _inject(monkeypatch, fake)
    settings = make_settings(REDIS_URL="redis://localhost:6379/0")

    await redis_hot.flush(settings=settings)

    assert "scan" in fake.calls
    assert not hasattr(fake, "keys_called")
