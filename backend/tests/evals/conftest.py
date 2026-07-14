"""Fixtures for F4 (evaluation harness) tests.

DB fixtures mirror `tests/rag/conftest.py` (own `engine` + the process-lifetime
`get_engine`/`get_sessionmaker` `@lru_cache` reset). Only the harness-persistence and compare tests
need Postgres; every suite test injects fake seams (F2/F3 dependency-injection style, no mock
library) and needs no DB — so the DB fixtures here are NOT autouse.

`make_settings()` builds a `Settings` bypassing the env file, with the F4 keys pointed at
test-controlled paths — the inline-`_settings` helper pattern F2/F3 use.
"""

import json
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

import app.db.engine as db_engine
import app.db.models  # noqa: F401 — registers all models on Base.metadata
from app.core.settings import Settings, settings

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "evals"


def _load_jsonl(name: str) -> list[dict]:
    path = FIXTURES_DIR / name
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def make_settings(**overrides) -> Settings:
    base = dict(
        _env_file=None,
        DATABASE_URL="postgresql+asyncpg://u:p@localhost:5432/db",
        ADMIN_EMAIL="a@b.c", ADMIN_PASSWORD="x",
        OPENAI_API_KEY="k", PINECONE_API_KEY="k", PINECONE_INDEX="i",
    )
    base.update(overrides)
    return Settings(**base)


class _FakeSessionCtx:
    """Async-context-manager stand-in for an AsyncSession — suite tests that don't touch the DB
    inject a sessionmaker returning this, so `async with sessionmaker() as s` works without DB."""

    async def __aenter__(self):
        return object()

    async def __aexit__(self, *exc):
        return False


def fake_sessionmaker():
    return _FakeSessionCtx()


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR


# ---- DB fixtures (only harness/compare tests request these; not autouse) ----


@pytest_asyncio.fixture
async def engine():
    eng: AsyncEngine = create_async_engine(str(settings.DATABASE_URL))
    yield eng
    db_engine.get_engine.cache_clear()
    db_engine.get_sessionmaker.cache_clear()
    await eng.dispose()


@pytest_asyncio.fixture
async def db_sessionmaker(engine):
    """A real async_sessionmaker bound to the test engine; truncates eval tables on teardown."""
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    from sqlalchemy import text

    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE TABLE eval_results, eval_runs CASCADE"))
