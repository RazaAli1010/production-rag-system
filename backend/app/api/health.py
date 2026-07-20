"""`GET /api/health` — per-dependency liveness (F11, AC-7).

Each dependency is probed concurrently and independently under its own short timeout, so one slow or
down dependency neither hangs the endpoint nor masks the others. Returns 200 when every *core*
dependency is up, 503 (naming the offender) otherwise. The OpenAI probe is presence-only — a health
check must never make a billable call.
"""

import asyncio

import anyio
import structlog
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.caching import redis_hot
from app.core.settings import settings
from app.db.session import get_session
from app.indexing.vectorstore import _client_and_host

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api", tags=["health"])

# 5s, not 2s: a cold Pinecone client pays TLS handshake + init (~2.1s measured) on its first call.
_PROBE_TIMEOUT_S = 5.0


async def _postgres(session: AsyncSession) -> str:
    await session.execute(text("SELECT 1"))
    return "ok"


async def _redis() -> str:
    client = redis_hot._client(settings)
    if client is None:
        return "skipped"  # Postgres-only deployment is valid, not a degradation (F9)
    await client.ping()
    return "ok"


def _pinecone_sync() -> None:
    # Sync client — validates key + index existence in one network call; runs off the loop.
    # Shares the retrieval path's cache, so probe and hot path warm ONE client between them.
    _client_and_host(settings.PINECONE_API_KEY.get_secret_value(), settings.PINECONE_INDEX)


async def _pinecone() -> str:
    await anyio.to_thread.run_sync(_pinecone_sync)
    return "ok"


async def _bm25() -> str:
    # Cheap stat, inline (async-mandate "cheap pure-CPU may run inline").
    return "ok" if settings.BM25_PATH.exists() else "missing"


async def _openai_key() -> str:
    return "ok" if settings.OPENAI_API_KEY.get_secret_value() else "missing"


async def _probe(coro) -> str:
    try:
        return await asyncio.wait_for(coro, timeout=_PROBE_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 — any failure means "down" for that dependency
        return f"down: {type(exc).__name__}"


@router.get("/health", summary="Per-dependency health check",
            description="Reports Pinecone, Postgres, Redis, BM25 and the OpenAI key. 200 when all "
                        "core dependencies are up, 503 otherwise.")
async def health(session: AsyncSession = Depends(get_session)) -> JSONResponse:
    postgres, redis, pinecone, bm25, openai_key = await asyncio.gather(
        _probe(_postgres(session)), _probe(_redis()), _probe(_pinecone()),
        _probe(_bm25()), _probe(_openai_key()),
    )
    deps = {"postgres": postgres, "redis": redis, "pinecone": pinecone,
            "bm25": bm25, "openai_key": openai_key}
    # Core = must be up to serve answers. Redis "skipped" (unconfigured) is fine; BM25 "missing"
    # means hybrid/degraded retrieval can't work, so it counts as down.
    core_ok = (postgres == "ok" and pinecone == "ok" and bm25 == "ok"
               and redis in ("ok", "skipped") and openai_key == "ok")
    status_code = 200 if core_ok else 503
    if not core_ok:
        logger.warning("api.health_degraded", **deps)
    return JSONResponse({"status": "ok" if core_ok else "degraded", "dependencies": deps},
                        status_code=status_code)
