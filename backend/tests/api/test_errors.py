"""T3 — the error envelope + handlers (AC-13). Handlers are unit-tested directly (routing them
each through a live failure is covered where natural: validation in test_ask_contract, 429 in
test_ratelimit, timeout/503 in test_timeout)."""

import asyncio

from starlette.requests import Request

from app.core import errors
from app.core.middleware import request_id_var
from app.rag.errors import ProviderError


def _req() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/api/ask", "headers": []})


def test_envelope_carries_request_id():
    token = request_id_var.set("rid-123")
    try:
        env = errors.envelope("x", "msg")
    finally:
        request_id_var.reset(token)
    assert env == {"error": {"type": "x", "message": "msg", "request_id": "rid-123"}}


async def test_provider_handler_503():
    r = await errors.provider_handler(_req(), ProviderError("upstream boom"))
    assert r.status_code == 503
    assert b"provider_unavailable" in r.body
    assert b"upstream boom" not in r.body  # internal detail never leaked


async def test_timeout_handler_504():
    r = await errors.timeout_handler(_req(), TimeoutError())
    assert r.status_code == 504
    assert b"timeout" in r.body


async def test_unhandled_handler_500_is_generic():
    r = await errors.unhandled_handler(_req(), ValueError("secret db string"))
    assert r.status_code == 500
    assert b"internal_error" in r.body
    assert b"secret db string" not in r.body  # never leak the raised message


async def test_rate_limited_handler_sets_retry_after():
    r = await errors.rate_limited_handler(_req(), errors.RateLimited(retry_after=42))
    assert r.status_code == 429
    assert r.headers["Retry-After"] == "42"


def test_asyncio_timeout_is_builtin():
    # The handler is registered against asyncio.TimeoutError; asyncio.timeout() raises it. In 3.11+
    # they are the same object, so registration and raise agree.
    assert asyncio.TimeoutError is TimeoutError
