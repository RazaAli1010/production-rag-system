import asyncio
import contextlib

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse

from app.api import ask, auth, documents, health, history, internal, sessions
from app.caching import redis_hot
from app.core import errors
from app.core.exceptions import AuthError
from app.core.middleware import RequestContextMiddleware
from app.core.ratelimit import RateLimited
from app.core.settings import settings
from app.observability.logging import configure_logging
from app.rag import rerank
from app.rag.errors import ProviderError

logger = structlog.get_logger(__name__)


async def _warm_rerank() -> None:
    # ~19s cold vs ~2.7s warm (measured): paid by the first request, it alone exceeded
    # REQUEST_TIMEOUT_S and timed the turn out. `get_rerank_model` is lock-guarded, so a request
    # arriving mid-load waits on this same load rather than starting a second one.
    try:
        await rerank.warm_rerank_model(settings)
        logger.info("startup.rerank_warm")
    except Exception as exc:  # noqa: BLE001 — warmup is an optimization, never a boot requirement
        logger.warning("startup.rerank_warm_failed", error=str(exc))


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI):
    configure_logging(settings)  # F13: the one structlog.configure, JSON logs + request_id (AC-7)
    # Backgrounded, not awaited: blocking boot on the weight load would delay readiness and risk
    # the platform's health-check window. Held in a local so the task isn't GC'd mid-flight.
    warm_task = asyncio.create_task(_warm_rerank()) if settings.ENABLE_RERANK else None
    yield
    if warm_task is not None:
        warm_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await warm_task
    # Drop the pooled redis.asyncio clients (limiter + F9 cache share them) on shutdown.
    await redis_hot.close()


app = FastAPI(title="CampusRAG", lifespan=_lifespan)

# Middleware. request_id must be OUTERMOST so every downstream log line and error envelope carries
# it; gzip and CORS are stdlib starlette (no new dependency).
app.add_middleware(GZipMiddleware, minimum_size=settings.GZIP_MIN_BYTES)
if settings.CORS_ALLOW_ORIGINS:
    # Never a wildcard with credentials — an explicit allowlist only (AC-15).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ALLOW_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
app.add_middleware(RequestContextMiddleware)

app.include_router(auth.router)
app.include_router(internal.router)
app.include_router(sessions.router)  # F17
app.include_router(ask.router)  # F17 + F11
app.include_router(health.router)  # F11
app.include_router(documents.router)  # F11
app.include_router(history.router)  # F11

# Typed error handlers → the uniform {error:{type,message,request_id}} envelope (F11, AC-13).
app.add_exception_handler(RequestValidationError, errors.validation_handler)
app.add_exception_handler(RateLimited, errors.rate_limited_handler)
app.add_exception_handler(ProviderError, errors.provider_handler)
app.add_exception_handler(asyncio.TimeoutError, errors.timeout_handler)
app.add_exception_handler(Exception, errors.unhandled_handler)


@app.exception_handler(AuthError)
async def _auth_error_handler(request: Request, exc: AuthError) -> JSONResponse:
    # The distinguishing detail goes to the log; the response body stays generic so the endpoint
    # is not an enumeration oracle.
    logger.info("auth.reject", reason=exc.reason, status=exc.status, path=request.url.path)
    headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None
    return JSONResponse({"detail": exc.detail}, status_code=exc.status, headers=headers)
