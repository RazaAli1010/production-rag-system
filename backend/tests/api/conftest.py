"""Fixtures for F11 (API hardening) tests.

Mirrors `tests/memory/conftest.py`: own engine/session + the process-lifetime `@lru_cache` reset
(asyncpg connections are loop-bound), a live Postgres at `DATABASE_URL`. The RAG pipeline
(`astream`) is faked per-test, so the suite makes zero OpenAI/Pinecone calls.
"""

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

import app.db.engine as db_engine
import app.db.models  # noqa: F401 — registers all models on Base.metadata
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
def api_settings():
    """Default posture: memory off (stateless /api/ask), rate-limit flag on but Redis unconfigured
    so the limiter is a no-op unless a test wires a fake client in."""
    return make_settings()


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


@pytest_asyncio.fixture(autouse=True)
async def _cleanup_tables(engine):
    """Truncate ONLY F11/F17-owned tables — never `documents`/`chunks` (the recorded cause of
    false_refusal_rate=1.0 in the f7/f8 reports). `documents` rows a test seeds are cleaned by that
    test itself so the corpus stays untouched here."""
    yield
    from app.memory import service

    await service.drain_writes()
    async with engine.begin() as conn:
        await conn.execute(
            text("TRUNCATE TABLE messages, sessions, refresh_tokens, request_logs CASCADE")
        )


@pytest_asyncio.fixture(autouse=True)
async def _reset_memory_locks():
    from app.memory import service

    service.reset_locks()
    yield
    service.reset_locks()


@pytest_asyncio.fixture
async def admin(sessionmaker_, api_settings):
    return await _seed_user(sessionmaker_, api_settings, role="admin")


@pytest_asyncio.fixture
async def student(sessionmaker_, api_settings):
    return await _seed_user(sessionmaker_, api_settings, role="student")


async def _seed_user(sessionmaker_, api_settings, *, role):
    import datetime as dt
    import uuid

    from app.core.security import encode_access
    from app.db.enums import UserRole
    from app.db.models.auth import RefreshToken
    from app.db.models.user import User

    async with sessionmaker_() as db:
        u = User(email=f"{uuid.uuid4().hex}@pu.edu.pk", hashed_password="x",
                 role=UserRole.admin if role == "admin" else UserRole.student)
        db.add(u)
        await db.flush()
        jti = str(uuid.uuid4())
        db.add(RefreshToken(
            user_id=u.id, jti=jti,
            expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(days=7),
        ))
        await db.commit()
        token = encode_access(u.id, u.role, jti, settings=api_settings)
        return {"headers": {"Authorization": f"Bearer {token}"}, "user_id": u.id}


@pytest_asyncio.fixture
async def client(sessionmaker_, api_settings, monkeypatch):
    """httpx ASGI client, module `settings` singletons patched to `api_settings` at each F11 import
    site, and the write-behind `get_sessionmaker` bound to the per-test engine."""
    from app.api import ask as ask_router
    from app.api import health as health_router
    from app.api import history as history_router
    from app.api import sessions as sessions_router
    from app.auth import deps
    from app.core import ratelimit
    from app.main import app

    for target in (ask_router, health_router, history_router, sessions_router, deps, ratelimit):
        monkeypatch.setattr(target, "settings", api_settings)
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


class Recorder:
    """Captures the args the faked pipeline received, so route-level tests can assert what F11
    passed down (flags, namespace, the deep-mode model)."""

    def __init__(self):
        self.flags = None
        self.namespace = None
        self.model = None
        self.session_id = None


def make_fake_astream(rec: Recorder, *, answer="Grounded answer [1]", delay: float = 0.0):
    from app.core.contracts import AnswerResponse, Citation, PipelineFlags
    from app.rag.events import SSEEvent

    async def _fake(question, *, k=5, namespace=None, flags=None, memory=None, session=None,
                    settings=None, sessionmaker=None, session_id=None):
        import asyncio

        rec.flags = flags
        rec.namespace = namespace
        rec.model = settings.LLM_MODEL if settings is not None else None
        rec.session_id = session_id
        if delay:
            await asyncio.sleep(delay)
        yield SSEEvent(event="stage", data={"stage": "searching", "status": "done", "ms": 1})
        for tok in answer.split(" "):
            yield SSEEvent(event="token", data={"token": tok + " "})
        cits = [Citation(chunk_id="d:0", doc_id="d", title="PU", url="http://x", quote="q")]
        resp = AnswerResponse(answer="", citations=cits,
                              pipeline_flags=flags or PipelineFlags(), session_id=session_id,
                              tokens_in=10, tokens_out=5)
        yield SSEEvent(event="citations", data={"citations": [c.model_dump() for c in cits]})
        yield SSEEvent(event="meta", data=resp.model_dump(exclude={"answer"}))
        yield SSEEvent(event="done", data={})

    return _fake


def parse_sse(text: str):
    import json

    out = []
    for block in text.strip().split("\n\n"):
        ev = data = None
        for line in block.splitlines():
            if line.startswith("event: "):
                ev = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if ev:
            out.append((ev, data))
    return out


class FakeRedis:
    """Minimal async fake for the fixed-window limiter: INCR / EXPIRE / TTL over a dict."""

    def __init__(self, *, fail: bool = False):
        self.store: dict[str, int] = {}
        self.ttls: dict[str, int] = {}
        self.fail = fail

    async def incr(self, key):
        if self.fail:
            raise ConnectionError("redis down")
        self.store[key] = self.store.get(key, 0) + 1
        return self.store[key]

    async def expire(self, key, ttl):
        self.ttls[key] = ttl

    async def ttl(self, key):
        return self.ttls.get(key, -1)
