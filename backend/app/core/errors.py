"""The single error envelope + typed exception handlers (F11, AC-13).

Every error the API returns has body `{"error": {"type", "message", "request_id"}}` and a safe
message — never a traceback or DB detail. Handlers are registered in `main.py`; the F10 `AuthError`
handler stays where it is (it already renders a non-oracle body).

Pinecone failure is deliberately absent: F5 degrades it to a `degraded=true` answer inside the
pipeline, so it never reaches a handler.
"""

import asyncio

import structlog
from fastapi import Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.core.middleware import request_id_var
from app.rag.errors import ProviderError

logger = structlog.get_logger(__name__)


class RateLimited(Exception):
    """Raised by the rate limiter when a tier's per-window budget is exceeded (AC-8/9)."""

    def __init__(self, retry_after: int) -> None:
        self.retry_after = retry_after
        super().__init__("rate limit exceeded")


def envelope(type_: str, message: str) -> dict:
    return {"error": {"type": type_, "message": message, "request_id": request_id_var.get()}}


def _json(status_code: int, type_: str, message: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(envelope(type_, message), status_code=status_code, headers=headers)


async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    # exc.errors() is safe field-level detail (loc/msg/type), not internal state — attach it under
    # the envelope so a client can see WHICH field failed while the shape stays uniform.
    body = envelope("validation_error", "Invalid request")
    body["error"]["detail"] = _safe_errors(exc.errors())
    return JSONResponse(body, status_code=status.HTTP_422_UNPROCESSABLE_ENTITY)


def _safe_errors(errors: list) -> list:
    # Drop `ctx`/`input`, which can echo the raw payload back — keep only loc/msg/type.
    return [{"loc": e.get("loc"), "msg": e.get("msg"), "type": e.get("type")} for e in errors]


async def rate_limited_handler(request: Request, exc: RateLimited) -> JSONResponse:
    return _json(status.HTTP_429_TOO_MANY_REQUESTS, "rate_limited",
                 "Too many requests. Slow down.",
                 headers={"Retry-After": str(exc.retry_after)})


async def provider_handler(request: Request, exc: ProviderError) -> JSONResponse:
    logger.warning("api.provider_unavailable", error=str(exc))
    return _json(status.HTTP_503_SERVICE_UNAVAILABLE, "provider_unavailable",
                 "An upstream model provider is temporarily unavailable.")


async def timeout_handler(request: Request, exc: asyncio.TimeoutError) -> JSONResponse:
    logger.warning("api.timeout", path=request.url.path)
    return _json(status.HTTP_504_GATEWAY_TIMEOUT, "timeout", "The request timed out.")


async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
    # The one place a stack trace could leak — log it, return a generic body (AC-13).
    logger.exception("api.unhandled_error", path=request.url.path)
    return _json(status.HTTP_500_INTERNAL_SERVER_ERROR, "internal_error",
                 "An internal error occurred.")
