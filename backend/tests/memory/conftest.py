"""Fixtures for F17 (session memory) tests.

DB fixtures mirror `tests/cache/conftest.py` (own `engine`/`session` + the process-lifetime
`get_engine`/`get_sessionmaker` `@lru_cache` reset — asyncpg connections are loop-bound and
pytest-asyncio gives each test its own loop). Requires a live Postgres reachable at `DATABASE_URL`.

The summariser LLM (`gpt-4o-mini`) is NEVER real here: tests inject a fake `ChatOpenAI` (F2's
dependency-injection style), so the suite — and the `memory:` CI job — need no OpenAI key with spend.
"""

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

import app.db.engine as db_engine
import app.db.models  # noqa: F401 — registers all models on Base.metadata
from app.core.settings import Settings
from app.core.settings import settings as global_settings
from app.db.session import get_session


def make_settings(**o) -> Settings:
    """A Settings instance with the required secrets stubbed — never the module-level singleton,
    which is built once at import time and can't reflect per-test overrides."""
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
def memory_settings():
    """Memory ON — the default posture for F17 tests."""
    return make_settings(ENABLE_MEMORY=True)


@pytest.fixture(autouse=True)
def _f17_settings_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("PINECONE_API_KEY", "pc-test")
    monkeypatch.setenv("PINECONE_INDEX", "campus-rag-test")
    monkeypatch.setenv("JWT_SECRET", "test-secret")


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
    """The `sessionmaker` the memory service takes for its own short-lived sessions (write-behind
    outlives the request session) — bound to the per-test engine, like F9's store."""
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def session(engine):
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
        await s.rollback()


@pytest_asyncio.fixture(autouse=True)
async def _cleanup_tables(engine):
    """Truncates `sessions`/`messages` ONLY — deliberately not `chunks`/`documents`.

    `tests/rag/conftest.py` truncates the corpus tables against this same DATABASE_URL, which is the
    recorded cause of `false_refusal_rate=1.0` in the f7/f8 eval reports. F17's tests seed their own
    sessions/messages and need no corpus, so this one does not add to that.
    """
    yield
    from sqlalchemy import text

    # Drain any in-flight write-behind assistant writes BEFORE truncating: a pending write holds a
    # row lock the TRUNCATE (ACCESS EXCLUSIVE) would otherwise block on, deadlocking teardown.
    from app.memory import service

    await service.drain_writes()
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE TABLE messages, sessions CASCADE"))


@pytest_asyncio.fixture(autouse=True)
async def _reset_memory_locks():
    """The per-session `asyncio.Lock` registry is process-lifetime; drop it between tests so a lock
    held (or leaked) by one test can't 409 the next. Locks are loop-bound, like asyncpg's."""
    from app.memory import service

    service.reset_locks()
    yield
    service.reset_locks()


@pytest_asyncio.fixture
async def authed(sessionmaker_, memory_settings):
    """A logged-in student: seeds a user + a live refresh token (the sid `resolve_jwt` checks) and
    mints a matching access token — no bcrypt, so authed tests stay fast. Returns headers + user_id."""
    import datetime as dt
    import uuid

    from app.core.security import encode_access
    from app.db.models.auth import RefreshToken
    from app.db.models.user import User

    async with sessionmaker_() as db:
        u = User(email=f"{uuid.uuid4().hex}@pu.edu.pk", hashed_password="x")
        db.add(u)
        await db.flush()
        jti = str(uuid.uuid4())
        db.add(RefreshToken(
            user_id=u.id, jti=jti,
            expires_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=7),
        ))
        await db.commit()
        token = encode_access(u.id, u.role, jti, settings=memory_settings)
        return {"headers": {"Authorization": f"Bearer {token}"}, "user_id": u.id}


@pytest_asyncio.fixture
async def client(sessionmaker_, memory_settings, monkeypatch):
    """httpx ASGI client with the session dependency bound to the per-test engine and the module
    `settings` singletons patched to `memory_settings` at each import site the routers read."""
    from app.api import ask as ask_router
    from app.api import sessions as sessions_router
    from app.auth import deps
    from app.main import app

    monkeypatch.setattr(ask_router, "settings", memory_settings)
    monkeypatch.setattr(sessions_router, "settings", memory_settings)
    monkeypatch.setattr(deps, "settings", memory_settings)
    # The write-behind (and the stateless path's cache seam) call get_sessionmaker() for a session
    # that outlives the request. Bind it to the per-test engine so it is the SAME engine as
    # everything else — otherwise the teardown disposes the app engine out from under an in-flight
    # write-behind task, which hangs. Mirrors how F9 injects its sessionmaker into the cache seam.
    monkeypatch.setattr(ask_router, "get_sessionmaker", lambda: sessionmaker_)

    async def _override_session():
        async with sessionmaker_() as s:
            yield s
            await s.commit()

    app.dependency_overrides[get_session] = _override_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()
