import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

import app.db.engine as db_engine
import app.db.models  # noqa: F401
from app.core.settings import Settings
from app.core.settings import settings as global_settings
from app.db.session import get_session


def make_settings(**o) -> Settings:
    return Settings(
        _env_file=None,
        **{
            "DATABASE_URL": str(global_settings.DATABASE_URL),
            "ADMIN_EMAIL": "a@b.c",
            "ADMIN_PASSWORD": "x",
            "OPENAI_API_KEY": "sk-test",
            "PINECONE_API_KEY": "pc-test",
            "PINECONE_INDEX": "campus-rag-test",
            "JWT_SECRET": "test-secret",
            **o,
        },
    )


@pytest.fixture
def auth_settings():
    """Cost 4, not the production 12: bcrypt is ~250ms per hash by design, which would put the
    suite in minutes. Tests that measure bcrypt itself (timing oracle, concurrency) build their own
    settings at a realistic cost."""
    return make_settings(BCRYPT_ROUNDS=4)


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
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def session(engine):
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
        await s.rollback()


@pytest_asyncio.fixture
async def client(sessionmaker_, auth_settings, monkeypatch):
    """The endpoints read the module-level `settings` singleton, so the test cost-4 bcrypt has to
    be patched in at each import site rather than injected."""
    from app.api import auth as auth_router
    from app.auth import deps
    from app.main import app

    monkeypatch.setattr(auth_router, "settings", auth_settings)
    monkeypatch.setattr(deps, "settings", auth_settings)

    async def _override_session():
        async with sessionmaker_() as s:
            yield s
            await s.commit()

    app.dependency_overrides[get_session] = _override_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest_asyncio.fixture(autouse=True)
async def _cleanup_tables(engine):
    """F10's four tables ONLY — never `documents`/`chunks`. `tests/rag/conftest.py` truncates the
    corpus against this same DATABASE_URL, which is the recorded cause of false_refusal_rate=1.0 in
    the f7/f8 eval reports. Do not add to that."""
    yield
    async with engine.begin() as conn:
        await conn.execute(
            text("TRUNCATE TABLE refresh_tokens, login_attempts, api_keys, users CASCADE")
        )
