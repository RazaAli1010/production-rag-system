import uuid
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.schemas import Principal, RegisterRequest
from app.core.exceptions import (
    BAD_CREDENTIALS,
    DUPLICATE_EMAIL,
    GENERIC,
    LOCKOUT,
    AuthError,
)
from app.core.security import (
    api_key_hash,
    decode_token,
    encode_access,
    encode_refresh,
    hash_password,
    new_api_key,
    verify_password,
)
from app.db.enums import UserRole
from app.db.models import ApiKey, LoginAttempt, RefreshToken, User

logger = structlog.get_logger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


async def register(session: AsyncSession, req: RegisterRequest, *, settings) -> User:
    allowlist = settings.AUTH_EMAIL_DOMAIN_ALLOWLIST
    if allowlist and not any(req.email.lower().endswith(d.lower()) for d in allowlist):
        raise AuthError(403, "Email domain not allowed", reason="domain_not_allowed")

    user = User(
        email=req.email,
        hashed_password=await hash_password(req.password, settings=settings),
        role=UserRole.student,
        is_active=True,
    )
    session.add(user)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise AuthError(409, DUPLICATE_EMAIL, reason="duplicate_email") from exc
    await session.refresh(user)
    logger.info("auth.register", user_id=str(user.id))
    return user


async def _recent_failures(session: AsyncSession, email: str, *, settings) -> int:
    since = _now() - timedelta(minutes=settings.LOGIN_LOCKOUT_WINDOW_MIN)
    return await session.scalar(
        select(func.count())
        .select_from(LoginAttempt)
        .where(
            LoginAttempt.email_or_ip == email,
            LoginAttempt.success.is_(False),
            LoginAttempt.attempted_at > since,
        )
    )


async def _issue_pair(session: AsyncSession, user: User, *, ip, user_agent, settings):
    refresh, jti, expires_at = encode_refresh(user.id, user.role, settings=settings)
    session.add(
        RefreshToken(
            user_id=user.id, jti=jti, expires_at=expires_at, ip=ip, user_agent=user_agent
        )
    )
    return encode_access(user.id, user.role, sid=jti, settings=settings), refresh


async def authenticate(
    session: AsyncSession,
    email: str,
    password: str,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
    settings,
) -> tuple[str, str]:
    if await _recent_failures(session, email, settings=settings) >= settings.LOGIN_MAX_FAILURES:
        raise AuthError(429, LOCKOUT, reason="lockout")

    user = await session.scalar(select(User).where(User.email == email))
    # Verify before branching on `user is None`: an early return would leak account existence by
    # timing no matter how generic the response body is.
    ok = await verify_password(password, user.hashed_password if user else None, settings=settings)

    if not (ok and user is not None and user.is_active):
        session.add(LoginAttempt(email_or_ip=email, success=False))
        await session.commit()
        raise AuthError(401, BAD_CREDENTIALS, reason="bad_credentials")

    session.add(LoginAttempt(email_or_ip=email, success=True))
    access, refresh = await _issue_pair(
        session, user, ip=ip, user_agent=user_agent, settings=settings
    )
    await session.commit()
    logger.info("auth.login", user_id=str(user.id))
    return access, refresh


async def rotate_refresh(
    session: AsyncSession,
    token: str,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
    settings,
) -> tuple[str, str]:
    claims = decode_token(token, expect="refresh", settings=settings)

    # FOR UPDATE: without it, two concurrent refreshes of one token both read it unrevoked and
    # each mint a valid family.
    old = await session.scalar(
        select(RefreshToken)
        .where(
            RefreshToken.jti == claims["jti"],
            RefreshToken.revoked_at.is_(None),
            RefreshToken.expires_at > func.now(),
        )
        .with_for_update()
    )
    if old is None:
        raise AuthError(401, GENERIC, reason="revoked_family")

    user = await session.get(User, old.user_id)
    if user is None or not user.is_active:
        raise AuthError(401, GENERIC, reason="inactive_user")

    refresh, jti, expires_at = encode_refresh(user.id, user.role, settings=settings)
    old.revoked_at = _now()
    old.replaced_by_jti = jti
    session.add(
        RefreshToken(
            user_id=user.id, jti=jti, expires_at=expires_at, ip=ip, user_agent=user_agent
        )
    )
    access = encode_access(user.id, user.role, sid=jti, settings=settings)
    await session.commit()
    logger.info("auth.rotate", user_id=str(user.id))
    return access, refresh


async def revoke_family(session: AsyncSession, sid: str) -> None:
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.jti == sid, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=_now())
    )
    await session.commit()
    logger.info("auth.logout")


async def resolve_jwt(session: AsyncSession, token: str, *, settings) -> Principal:
    claims = decode_token(token, expect="access", settings=settings)
    try:
        user_id = uuid.UUID(claims["sub"])
    except (KeyError, ValueError) as exc:
        raise AuthError(401, GENERIC, reason="bad_token") from exc

    user = await session.scalar(
        select(User)
        .join(RefreshToken, RefreshToken.user_id == User.id)
        .where(
            RefreshToken.jti == claims.get("sid"),
            RefreshToken.revoked_at.is_(None),
            RefreshToken.expires_at > func.now(),
            User.id == user_id,
            User.is_active.is_(True),
        )
    )
    if user is None:
        raise AuthError(401, GENERIC, reason="revoked_family")

    # role comes from the row, not the token's claim: a demoted admin must not keep admin until
    # their access token expires.
    return Principal(kind=user.role.value, user_id=user.id, email=user.email, role=user.role)


async def resolve_api_key(session: AsyncSession, raw: str, *, settings) -> Principal:
    # ponytail: no index on key_hash — a seq scan over single-digit bot keys beats one. Add a
    # unique index (and a migration) if keys ever reach the thousands.
    row = (
        await session.execute(
            select(User, ApiKey)
            .join(ApiKey, ApiKey.user_id == User.id)
            .where(
                ApiKey.key_hash == api_key_hash(raw),
                ApiKey.revoked_at.is_(None),
                User.is_active.is_(True),
            )
        )
    ).first()
    if row is None:
        raise AuthError(401, GENERIC, reason="bad_api_key")

    user, key = row
    return Principal(
        kind="api_key", user_id=user.id, email=user.email, role=user.role, api_key_id=key.id
    )


async def prune(session: AsyncSession, *, settings) -> tuple[int, int]:
    window_start = _now() - timedelta(minutes=settings.LOGIN_LOCKOUT_WINDOW_MIN)
    tokens = await session.execute(
        delete(RefreshToken).where(RefreshToken.expires_at < _now())
    )
    attempts = await session.execute(
        delete(LoginAttempt).where(LoginAttempt.attempted_at < window_start)
    )
    await session.commit()
    logger.info("auth.prune", refresh_tokens=tokens.rowcount, login_attempts=attempts.rowcount)
    return tokens.rowcount, attempts.rowcount


async def issue_key(session: AsyncSession, email: str, label: str) -> str:
    user = await session.scalar(select(User).where(User.email == email))
    if user is None:
        raise AuthError(404, "No such user", reason="unknown_user")

    raw, hashed = new_api_key()
    session.add(ApiKey(user_id=user.id, key_hash=hashed, label=label))
    await session.commit()
    return raw


async def revoke_key(session: AsyncSession, label: str) -> int:
    result = await session.execute(
        update(ApiKey)
        .where(ApiKey.label == label, ApiKey.revoked_at.is_(None))
        .values(revoked_at=_now())
    )
    await session.commit()
    return result.rowcount
