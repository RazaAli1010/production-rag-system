"""T-3, T-4: engine connectivity + connect_args, and the get_session commit/rollback contract."""

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import _connect_args, get_engine
from app.db.models import User
from app.db.session import get_session


@pytest.mark.asyncio
async def test_select_1(engine):
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT 1"))
        assert result.scalar_one() == 1


def test_connect_args_disable_statement_cache_when_configured():
    # DB_STATEMENT_CACHE_SIZE defaults to 0 (design.md §6) -> pgbouncer-safe.
    args = _connect_args()
    assert args == {"statement_cache_size": 0, "prepared_statement_cache_size": 0}


def test_get_engine_is_cached():
    assert get_engine() is get_engine()


def _build_app():
    app = FastAPI()

    @app.post("/commit-ok/{email}")
    async def commit_ok(email: str, session: AsyncSession = Depends(get_session)):
        session.add(User(email=email, hashed_password="x"))
        return {"ok": True}

    @app.post("/commit-fail/{email}")
    async def commit_fail(email: str, session: AsyncSession = Depends(get_session)):
        session.add(User(email=email, hashed_password="x"))
        await session.flush()
        raise RuntimeError("boom")

    return app


@pytest.mark.asyncio
async def test_get_session_commits_on_success(session, unique_email):
    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/commit-ok/{unique_email}")
    assert resp.status_code == 200

    row = await session.scalar(select(User).where(User.email == unique_email))
    assert row is not None
    await session.delete(row)
    await session.commit()


@pytest.mark.asyncio
async def test_get_session_rolls_back_on_exception(session, unique_email):
    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with pytest.raises(Exception):
            await client.post(f"/commit-fail/{unique_email}")

    row = await session.scalar(select(User).where(User.email == unique_email))
    assert row is None
