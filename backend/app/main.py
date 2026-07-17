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
from app.rag.errors import ProviderError

logger = structlog.get_logger(__name__)


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI):
    yield
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
