"""Redis hot tier — exact-match `sha256(normalized_query)` lookups (design.md §3, AC-1..AC-4).

Checked BEFORE the query is embedded, which is the whole point: an exact repeat costs one Redis
round-trip (~5-30ms) and never touches OpenAI. The semantic tier behind it needs an embedding first,
so it lands at ~150-250ms — still under the 300ms bar, but an order of magnitude slower than this.

**Every function here is fail-open.** The cache is an optimization, never a failure source (AC-3):
Upstash's free tier can and will disappear mid-request, and when it does the correct behaviour is a
slower answer, not a 5xx. So each entry point catches `Exception` and returns the miss value. That
is deliberately broader than catching `redis.RedisError` — a DNS failure, a TLS error or a bad
payload are all "the hot tier is unavailable" from the caller's point of view, and none of them are
worth failing a student's question over.

Async-mandate placement: `redis.asyncio` only. The `caching:` CI job bans bare `import redis` and
`from redis import ...` here, which is why the import below is `import redis.asyncio as redis`.
"""

import asyncio
import json

import redis.asyncio as redis
import structlog

logger = structlog.get_logger(__name__)

# One client per event loop, keyed by URL. redis.asyncio pools connections internally, so building a
# fresh client per request would throw away the pool and pay a new TCP+TLS handshake every lookup —
# on a 250ms budget that is the difference between a hot hit and a timeout.
_CLIENTS: dict[str, redis.Redis] = {}


def _client(settings) -> redis.Redis | None:
    """The pooled client, or None when Redis is not configured (AC-4).

    `REDIS_URL is None` short-circuits BEFORE construction and without logging: an unconfigured hot
    tier is a valid, supported deployment (Postgres-only), not a degradation, and logging it would
    emit one warning per request forever.
    """
    if settings.REDIS_URL is None:
        return None
    url = str(settings.REDIS_URL)
    client = _CLIENTS.get(url)
    if client is None:
        client = redis.from_url(url, decode_responses=True)
        _CLIENTS[url] = client
    return client


async def get(key: str, *, settings) -> dict | None:
    """The cached payload, or None on miss / unavailable / unparseable."""
    client = _client(settings)
    if client is None:
        return None
    try:
        async with asyncio.timeout(settings.CACHE_REDIS_TIMEOUT_S):
            raw = await client.get(key)
    except Exception as exc:  # noqa: BLE001 — fail-open: a cache outage must not fail the request
        logger.warning("rag.cache_degraded", tier="redis", op="get", error=str(exc))
        return None
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError) as exc:
        # A corrupt value is worse than no value: drop it so the next request re-populates cleanly.
        logger.warning("rag.cache_degraded", tier="redis", op="parse", error=str(exc))
        await delete(key, settings=settings)
        return None


async def set(key: str, payload: dict, *, settings) -> None:  # noqa: A001 — mirrors the Redis verb
    """Write with `CACHE_REDIS_TTL_S` expiry. Fire-and-forget from the caller's perspective: a
    failure is logged and swallowed (AC-19)."""
    client = _client(settings)
    if client is None:
        return
    try:
        async with asyncio.timeout(settings.CACHE_REDIS_TIMEOUT_S):
            await client.set(key, json.dumps(payload), ex=settings.CACHE_REDIS_TTL_S)
    except Exception as exc:  # noqa: BLE001 — fail-open
        logger.warning("rag.cache_write_failed", tier="redis", error=str(exc))


async def delete(key: str, *, settings) -> None:
    client = _client(settings)
    if client is None:
        return
    try:
        async with asyncio.timeout(settings.CACHE_REDIS_TIMEOUT_S):
            await client.delete(key)
    except Exception as exc:  # noqa: BLE001 — fail-open
        logger.warning("rag.cache_degraded", tier="redis", op="delete", error=str(exc))


async def flush(*, settings) -> int:
    """Delete every `CACHE_KEY_PREFIX*` key. Returns the count deleted (0 when Redis is absent).

    Uses async `SCAN` (via `scan_iter`), never `KEYS`: `KEYS` blocks the Redis server for the whole
    keyspace scan, and this runs against a shared free-tier instance. No timeout wraps the loop —
    unlike a lookup, `--flush` is an operator action with no latency budget, and cutting it off
    halfway would leave the cache half-flushed, which is worse than slow.
    """
    client = _client(settings)
    if client is None:
        return 0
    deleted = 0
    try:
        async for key in client.scan_iter(match=f"{settings.CACHE_KEY_PREFIX}*"):
            deleted += await client.delete(key)
    except Exception as exc:  # noqa: BLE001 — fail-open: --flush must still clear Postgres
        logger.warning("rag.cache_degraded", tier="redis", op="flush", error=str(exc))
    return deleted


async def close(*, settings=None) -> None:
    """Drop the pooled clients. Tests call this between loops (redis.asyncio connections are
    loop-bound, exactly like asyncpg's); F11's shutdown hook will call it in prod."""
    for client in list(_CLIENTS.values()):
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001 — teardown is best-effort
            pass
    _CLIENTS.clear()
