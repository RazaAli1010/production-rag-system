"""T-5: User/ApiKey/RefreshToken/LoginAttempt CRUD + uniqueness constraints."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db.models import ApiKey, LoginAttempt, RefreshToken, User


@pytest.mark.asyncio
async def test_user_crud(session, unique_email):
    user = User(email=unique_email, hashed_password="hash")
    session.add(user)
    await session.flush()

    fetched = await session.get(User, user.id)
    assert fetched.email == unique_email

    fetched.is_active = False
    await session.flush()
    refetched = await session.get(User, user.id)
    assert refetched.is_active is False

    await session.delete(refetched)
    await session.flush()
    assert await session.get(User, user.id) is None


@pytest.mark.asyncio
async def test_duplicate_email_raises_integrity_error(session, unique_email):
    session.add(User(email=unique_email, hashed_password="hash"))
    await session.flush()

    session.add(User(email=unique_email, hashed_password="hash2"))
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


@pytest.mark.asyncio
async def test_api_key_crud_and_cascade(session, unique_email):
    user = User(email=unique_email, hashed_password="hash")
    session.add(user)
    await session.flush()

    key = ApiKey(user_id=user.id, key_hash="hash123", label="cli")
    session.add(key)
    await session.flush()

    fetched = await session.get(ApiKey, key.id)
    assert fetched.user_id == user.id

    await session.delete(user)
    await session.flush()
    # populate_existing bypasses the identity map so the assert reflects the DB-level cascade,
    # not a stale Python object left over from the `.get()` call above.
    assert await session.get(ApiKey, key.id, populate_existing=True) is None  # FK CASCADE


@pytest.mark.asyncio
async def test_refresh_token_crud_and_duplicate_jti(session, unique_email):
    user = User(email=unique_email, hashed_password="hash")
    session.add(user)
    await session.flush()

    jti = f"jti-{uuid.uuid4().hex}"
    now = datetime.now(UTC)
    token = RefreshToken(
        user_id=user.id, jti=jti, issued_at=now, expires_at=now + timedelta(days=7)
    )
    session.add(token)
    await session.flush()

    fetched = await session.get(RefreshToken, token.id)
    assert fetched.jti == jti
    assert fetched.revoked_at is None

    session.add(
        RefreshToken(user_id=user.id, jti=jti, issued_at=now, expires_at=now + timedelta(days=7))
    )
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


@pytest.mark.asyncio
async def test_login_attempt_crud(session):
    attempt = LoginAttempt(email_or_ip="192.0.2.1", success=False)
    session.add(attempt)
    await session.flush()

    fetched = await session.get(LoginAttempt, attempt.id)
    assert fetched.success is False

    result = await session.scalars(
        select(LoginAttempt).where(LoginAttempt.email_or_ip == "192.0.2.1")
    )
    assert len(result.all()) >= 1
