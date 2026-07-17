"""Fixtures for F9 (semantic cache) tests.

DB fixtures mirror `tests/rag/conftest.py` (own `engine`/`session` + the process-lifetime
`get_engine`/`get_sessionmaker` `@lru_cache` reset — asyncpg connections are loop-bound and
pytest-asyncio gives each test its own loop). Requires a live Postgres reachable at `DATABASE_URL`.

Redis is NEVER real here: every test injects a fake client (F2's dependency-injection style, not a
mock library), so the suite — and the `caching:` CI job — need no Redis service. `redis_hot`'s
fail-open behaviour is exactly what makes that honest rather than a shortcut.
"""

import math

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

import app.db.engine as db_engine
import app.db.models  # noqa: F401 — registers all models on Base.metadata
from app.core.contracts import AnswerResponse, Citation, PipelineFlags
from app.core.settings import Settings
from app.core.settings import settings as global_settings

# A fixed manifest id for the whole suite: tests assert the invalidation RULE, so they must not
# depend on whatever index_manifest.json happens to be on disk.
MANIFEST = "manifest-abc123"


def make_settings(**o) -> Settings:
    """A Settings instance with the required secrets stubbed — never the module-level singleton,
    which is built once at import time and can't reflect per-test overrides."""
    return Settings(
        _env_file=None,
        DATABASE_URL=str(global_settings.DATABASE_URL),
        ADMIN_EMAIL="a@b.c",
        ADMIN_PASSWORD="x",
        OPENAI_API_KEY="sk-test",
        PINECONE_API_KEY="pc-test",
        PINECONE_INDEX="campus-rag-test",
        **o,
    )


@pytest.fixture
def cache_settings():
    """Cache ON, Redis OFF (Postgres-only tier) — the default posture for store tests."""
    return make_settings(ENABLE_CACHE=True, REDIS_URL=None)


@pytest.fixture
def settings_with_manifest(cache_settings, monkeypatch):
    """`cache_settings` + a pinned `manifest_id`, so entries written by a test are manifest-current
    without needing a real index on disk."""
    from app.caching import store

    async def _fake_manifest_id(settings):
        return MANIFEST

    monkeypatch.setattr(store, "manifest_id", _fake_manifest_id)
    return cache_settings


# --------------------------------------------------------------------------- vector helpers
# Synthetic unit vectors, not real embeddings: the store tests assert the RULE (cosine AND lexical
# AND manifest), and a hand-built vector makes the intended cosine exact and a failure legible.
# Whether the thresholds separate real questions is a different question — that is test_adversarial
# (T7), against real embeddings.


def unit(*components: float) -> list[float]:
    """A 1536-dim unit vector whose leading components are `components`."""
    vec = [0.0] * 1536
    for i, c in enumerate(components):
        vec[i] = c
    norm = math.sqrt(sum(c * c for c in vec)) or 1.0
    return [c / norm for c in vec]


def rotated(vec: list[float], cosine: float) -> list[float]:
    """A unit vector at exactly `cosine` from `vec` (rotated into an otherwise unused dimension)."""
    out = [c * cosine for c in vec]
    out[1535] = math.sqrt(max(0.0, 1 - cosine**2))
    return out


def make_answer(text: str = "Probation is cleared at CGPA 2.0 [1].") -> AnswerResponse:
    return AnswerResponse(
        answer=text,
        citations=[Citation(chunk_id="d:0", doc_id="d", title="PU Calendar",
                            url="http://x", quote="CGPA 2.0")],
        refused=False,
        pipeline_flags=PipelineFlags(cache=True),
        tokens_in=1200,
        tokens_out=80,
    )


@pytest.fixture(autouse=True)
def _f9_settings_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("PINECONE_API_KEY", "pc-test")
    monkeypatch.setenv("PINECONE_INDEX", "campus-rag-test")


@pytest_asyncio.fixture
async def engine():
    eng: AsyncEngine = create_async_engine(str(global_settings.DATABASE_URL))
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
async def sessionmaker_(engine):
    """The `sessionmaker` the cache seam takes — F9's store is given one rather than importing the
    app-wide singleton, so tests bind it to the per-test engine."""
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def session(engine):
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
        await s.rollback()


@pytest_asyncio.fixture(autouse=True)
async def _cleanup_tables(engine):
    """Truncates `cache_entries` ONLY — deliberately not `chunks`/`documents`.

    `tests/rag/conftest.py` truncates the corpus tables, and since the suite runs against the same
    DATABASE_URL as dev/eval runs, running the tests wipes the local corpus. `citations.
    parse_citations` then resolves no `[n]` marker, so the post-LLM gate refuses EVERY answer —
    which is what `false_refusal_rate = 1.0` in docs/eval_results/f8-compression-after-*.md is
    recording. F9's tests seed no corpus and need none, so this one does not add to that.
    """
    yield
    from sqlalchemy import text

    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE TABLE cache_entries CASCADE"))


@pytest_asyncio.fixture(autouse=True)
async def _reset_cache_singleton():
    """The process-lifetime `SemanticCache` holds a loaded matrix; without this reset one test's
    entries leak into the next test's cosine search. Redis clients are loop-bound (like asyncpg's),
    so they are dropped here too."""
    from app.caching import redis_hot, store

    await store.reset()
    yield
    await store.reset()
    await redis_hot.close()
