"""Regression: `get_index` must pay Pinecone's blocking `describe_index` handshake ONCE per
process, off the event loop.

It used to run sync and uncached on every `_retrieve_namespace` / `hydrate_sparse_only` call. A
blocking syscall on the loop thread means `asyncio.timeout(REQUEST_TIMEOUT_S)` in the /api/ask
handler never gets scheduled, so requests died at 30s with "request timed out" and every stage
still open, and /api/health's own probe tripped on the frozen loop and reported pinecone "down".
"""

import pinecone
import pytest

from app.core.settings import Settings
from app.indexing import vectorstore


def _settings(**o):
    return Settings(
        _env_file=None,
        DATABASE_URL="postgresql+asyncpg://u:p@localhost:5432/db",
        ADMIN_EMAIL="a@b.c", ADMIN_PASSWORD="x",
        OPENAI_API_KEY="k", PINECONE_API_KEY="k", PINECONE_INDEX="i",
        **o,
    )


class _FakeDescription:
    host = "fake-host"


class _FakePinecone:
    describe_calls = 0

    def __init__(self, api_key):
        self.api_key = api_key

    def describe_index(self, name):
        _FakePinecone.describe_calls += 1
        return _FakeDescription()

    def IndexAsyncio(self, host):  # noqa: N802 — mirrors the real client's method name
        return f"index@{host}"


@pytest.fixture
def fake_pinecone(monkeypatch):
    # lru_cache is process-wide: clear on both sides so this test neither inherits nor leaks state.
    vectorstore._client_and_host.cache_clear()
    monkeypatch.setattr(pinecone, "Pinecone", _FakePinecone)
    _FakePinecone.describe_calls = 0
    yield
    vectorstore._client_and_host.cache_clear()


async def test_handshake_paid_once_across_calls(fake_pinecone):
    settings = _settings()
    first = await vectorstore.get_index(settings)
    second = await vectorstore.get_index(settings)

    assert first == second == "index@fake-host"
    assert _FakePinecone.describe_calls == 1  # not once per call, as it was before


async def test_handshake_runs_off_the_event_loop(fake_pinecone):
    import threading

    seen = []
    loop_thread = threading.get_ident()
    original = _FakePinecone.describe_index

    def _record(self, name):
        seen.append(threading.get_ident())
        return original(self, name)

    _FakePinecone.describe_index = _record
    try:
        await vectorstore.get_index(_settings())
    finally:
        _FakePinecone.describe_index = original

    assert seen and loop_thread not in seen  # blocking call never ran on the loop thread
