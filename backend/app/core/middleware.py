"""Request correlation + timing middleware (F11, AC-14/15).

Every request gets a `request_id` (inbound `X-Request-ID` honored, else generated) stored in a
`ContextVar` so every `structlog` line and the error envelope carry it without threading it through
call signatures. The id and the elapsed wall-clock land on response headers; the ask route also
reads the same contextvar to stamp `AnswerResponse.request_id`.

CORS and gzip are stdlib Starlette middleware wired in `main.py` — not here.
"""

import time
import uuid
from contextvars import ContextVar

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.observability.metrics import RequestMetrics, metrics_var

# Default kept non-None so a log emitted outside any request (startup, a bare task) still renders a
# value rather than raising LookupError.
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

REQUEST_ID_HEADER = "x-request-id"
RESPONSE_TIME_HEADER = "x-response-time"


class RequestContextMiddleware:
    """Pure-ASGI (not BaseHTTPMiddleware) so it never buffers the SSE stream: it only mutates the
    response *start* headers and lets every body chunk pass straight through."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        rid = _inbound_id(scope) or uuid.uuid4().hex
        token = request_id_var.set(rid)
        # F13: fresh metrics accumulator per request; the pipeline feeds it, the ask route reads it.
        metrics_token = metrics_var.set(RequestMetrics())
        structlog.contextvars.bind_contextvars(request_id=rid)
        start = time.perf_counter()

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                elapsed_ms = int((time.perf_counter() - start) * 1000)
                headers = message.setdefault("headers", [])
                headers.append((REQUEST_ID_HEADER.encode(), rid.encode()))
                headers.append((RESPONSE_TIME_HEADER.encode(), str(elapsed_ms).encode()))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            structlog.contextvars.unbind_contextvars("request_id")
            request_id_var.reset(token)
            metrics_var.reset(metrics_token)


def _inbound_id(scope: Scope) -> str | None:
    for name, value in scope.get("headers", []):
        if name == REQUEST_ID_HEADER.encode():
            v = value.decode().strip()
            return v or None
    return None
