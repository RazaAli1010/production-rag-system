"""Fixtures for F1 ingestion tests.

DB fixtures mirror `tests/db/conftest.py` (own `engine`/`session` + the process-lifetime
`get_engine`/`get_sessionmaker` `@lru_cache` reset — asyncpg connections are loop-bound and
pytest-asyncio gives each test its own loop). Requires a live Postgres reachable at
`DATABASE_URL` (same as `tests/db`).
"""

from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

import app.db.engine as db_engine
import app.db.models  # noqa: F401 — registers all models on Base.metadata
from app.core.settings import settings

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "ingestion"


@pytest_asyncio.fixture
async def engine():
    eng: AsyncEngine = create_async_engine(str(settings.DATABASE_URL))
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture(autouse=True)
async def _reset_app_engine_cache():
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


@pytest_asyncio.fixture(autouse=True)
async def _cleanup_documents_table(engine):
    """Unlike `tests/db`, several ingestion tests (`test_run.py`, `test_acceptance.py`,
    `test_status.py`) exercise `run.main()`'s own internally-opened sessions and *commit* real
    `documents` rows (that's the point — verifying DB persistence across process-like
    boundaries) rather than using the roll-back-safe `session` fixture. Truncate after every
    test so committed rows never leak into the next test or pollute `alembic downgrade` (a
    downgrade that relaxes-then-tightens a NOT NULL column fails if leftover rows violate it)."""
    yield
    from sqlalchemy import text

    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE TABLE documents CASCADE"))


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture
def tmp_ingest_dirs(tmp_path, monkeypatch):
    """Point Settings' data dirs at a scratch tmp_path so tests never touch real app/data."""
    data_dir = tmp_path / "data"
    raw_dir = data_dir / "raw"
    extracted_dir = data_dir / "extracted"
    raw_dir.mkdir(parents=True)
    extracted_dir.mkdir(parents=True)

    monkeypatch.setattr(settings, "DATA_DIR", data_dir)
    monkeypatch.setattr(settings, "RAW_DIR", raw_dir)
    monkeypatch.setattr(settings, "EXTRACTED_DIR", extracted_dir)
    monkeypatch.setattr(settings, "SOURCES_CSV", data_dir / "sources.csv")
    monkeypatch.setattr(settings, "INGESTION_REPORT_DIR", tmp_path / "docs")
    return data_dir
