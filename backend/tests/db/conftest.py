"""Fixtures for F12 DB tests — require a live Postgres reachable at DATABASE_URL.

CI provides this via a Postgres service container (see .github/workflows/ci.yml); locally,
`make db-up && make migrate` before running `pytest tests/db`.
"""

import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)

import app.db.engine as db_engine
import app.db.models  # noqa: F401 — registers all models on Base.metadata
from app.core.settings import settings


@pytest.fixture(autouse=True)
def _f2_settings_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("PINECONE_API_KEY", "pc-test")
    monkeypatch.setenv("PINECONE_INDEX", "campus-rag-test")


@pytest_asyncio.fixture
async def engine():
    eng: AsyncEngine = create_async_engine(str(settings.DATABASE_URL))
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture(autouse=True)
async def _reset_app_engine_cache():
    """`app.db.engine.get_engine`/`get_sessionmaker` are process-lifetime @lru_cache singletons
    by design (correct for a real ASGI server: one process, one event loop). pytest-asyncio
    gives each test function its own event loop, and asyncpg connections are loop-bound, so a
    cached pool checked out under one test's loop can't be reused by the next. Reset the cache
    (and dispose the stale pool) after every test so each test's `get_session` calls bind to its
    own loop."""
    yield
    try:
        eng = db_engine.get_engine()
    except Exception:
        eng = None
    db_engine.get_engine.cache_clear()
    db_engine.get_sessionmaker.cache_clear()
    if eng is not None:
        await eng.dispose()


@pytest_asyncio.fixture
async def session(engine):
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as s:
        yield s
        await s.rollback()  # keep each test's writes isolated


@pytest.fixture
def unique_email():
    return f"user-{uuid.uuid4().hex[:8]}@example.com"
