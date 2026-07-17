"""Per-tier Redis fixed-window rate limiter (F11, AC-8/9/10/11/12).

Why Redis and not an in-process counter: an in-memory limiter lives per uvicorn worker and per
Render replica, so with `w` workers a client actually gets `w × limit`, and the effective ceiling
drifts silently as we scale. Redis is the ONE counter every replica increments, so the limit is the
limit regardless of topology.

Tiers come from F10's `auth.deps.rate_tier()` (keyed by user / api-key / ip), reused verbatim — F11
adds no tier logic. The client is F9's pooled `redis.asyncio` (`caching.redis_hot._client`), reused
so we hold one connection pool, not two.

ponytail: fixed window allows up to a 2× burst across a boundary (limit at 0:59, limit again at
1:00). Acceptable for a student read API; swap to a sliding window only if abuse is observed.
"""

import time

import structlog
from fastapi import Depends, Request

from app.auth.deps import client_ip, get_current_user_optional, rate_tier
from app.auth.schemas import Principal
from app.caching import redis_hot
from app.core.errors import RateLimited
from app.core.settings import settings

logger = structlog.get_logger(__name__)

_KEY_PREFIX = "campusrag:rl:"


async def check(bucket: str, limit: int, *, redis, window_s: int) -> None:
    """Count this request against the current window; raise `RateLimited` if over `limit`.

    Fails OPEN on any Redis error (AC-11): a limiter outage must never take the API down. The
    window id lives IN the key, so old windows expire by TTL with no sweep job.
    """
    key = f"{_KEY_PREFIX}{bucket}:{int(time.time()) // window_s}"
    try:
        n = await redis.incr(key)
        if n == 1:
            await redis.expire(key, window_s)
    except Exception as exc:  # noqa: BLE001 — fail open: a redis outage is not a request failure
        logger.warning("ratelimit.redis_unavailable", error=str(exc))
        return
    if n > limit:
        ttl = await redis.ttl(key)
        logger.info("ratelimit.reject", bucket=bucket, limit=limit)
        raise RateLimited(retry_after=max(ttl, 1))
    logger.debug("ratelimit.allow", bucket=bucket, count=n, limit=limit)


async def rate_limit_dep(
    request: Request,
    principal: Principal | None = Depends(get_current_user_optional),
) -> None:
    """FastAPI dependency for rate-limited routes. No-op when the flag is off or Redis is
    unconfigured (local dev / CI without Redis stays open, AC-12)."""
    if not settings.ENABLE_RATE_LIMIT:
        return
    redis = redis_hot._client(settings)
    if redis is None:
        return
    bucket, limit = rate_tier(principal, client_ip(request))
    await check(bucket, limit, redis=redis, window_s=settings.RATE_LIMIT_WINDOW_S)
