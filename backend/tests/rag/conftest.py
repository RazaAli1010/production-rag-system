"""Fixtures for F3 (baseline RAG chain) tests.

DB fixtures mirror `tests/indexing/conftest.py` (own `engine`/`session` + the process-lifetime
`get_engine`/`get_sessionmaker` `@lru_cache` reset — asyncpg connections are loop-bound and
pytest-asyncio gives each test its own loop). Requires a live Postgres reachable at
`DATABASE_URL` (same as `tests/indexing`).

Pinecone/OpenAI/ChatOpenAI are never real here — every test injects a fake client (F2's
dependency-injection style, not a mock library).
"""

import json
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

import app.db.engine as db_engine
import app.db.models  # noqa: F401 — registers all models on Base.metadata
from app.core.settings import settings
from app.db.models.corpus import Chunk as ChunkRow
from app.db.models.corpus import Document as DocRow

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "rag"


def _load_jsonl(name: str) -> list[dict]:
    path = FIXTURES_DIR / name
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


@pytest.fixture(autouse=True)
def _f3_settings_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("PINECONE_API_KEY", "pc-test")
    monkeypatch.setenv("PINECONE_INDEX", "campus-rag-test")


@pytest.fixture(autouse=True)
def _langfuse_absent(monkeypatch):
    """Confirms F3's default posture: no Langfuse creds configured. `observability.
    langfuse_handler()` must return `None` (no callback attached) in this state — Langfuse is
    optional, never a hard boot requirement (see Settings LANGFUSE_* defaults)."""
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)


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
async def _cleanup_tables(engine):
    yield
    from sqlalchemy import text

    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE TABLE chunks, documents CASCADE"))


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture
def smoke_questions() -> list[dict]:
    return _load_jsonl("smoke_questions.jsonl")


@pytest_asyncio.fixture
async def seeded_corpus(session):
    """Loads the committed `documents.jsonl`/`chunks.jsonl` fixtures (≥2 PU docs, ≥1 HEC doc)
    into Postgres via the same async-session pattern F2's tests use — documents must be flushed
    before chunks (no `relationship()` between the two models for SQLAlchemy to infer insert
    ordering from, see tests/rag/test_citations.py's `_seed` docstring)."""
    for row in _load_jsonl("documents.jsonl"):
        session.add(DocRow(**row))
    await session.flush()
    for row in _load_jsonl("chunks.jsonl"):
        session.add(ChunkRow(**row))
    await session.flush()
    return session
