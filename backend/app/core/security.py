import functools
import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

import anyio
import jwt
from passlib.context import CryptContext

from app.core.exceptions import GENERIC, RESET_INVALID, AuthError

API_KEY_PREFIX = "crag_"

@functools.lru_cache(maxsize=4)
def _ctx(rounds: int) -> CryptContext:
    return CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=rounds)


@functools.lru_cache(maxsize=4)
def _dummy_hash(rounds: int) -> str:
    """Verified against when an email is unknown, so a miss costs the same bcrypt round-trip as a
    wrong password. Keyed on rounds so its cost tracks the real hashes it stands in for."""
    return _ctx(rounds).hash(secrets.token_urlsafe(32))


async def hash_password(password: str, *, settings) -> str:
    return await anyio.to_thread.run_sync(_ctx(settings.BCRYPT_ROUNDS).hash, password)


async def verify_password(password: str, hashed: str | None, *, settings) -> bool:
    rounds = settings.BCRYPT_ROUNDS
    target = hashed if hashed is not None else _dummy_hash(rounds)
    return await anyio.to_thread.run_sync(_ctx(rounds).verify, password, target)


def api_key_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def new_api_key() -> tuple[str, str]:
    raw = API_KEY_PREFIX + secrets.token_urlsafe(32)
    return raw, api_key_hash(raw)


def _encode(claims: dict, *, settings) -> str:
    return jwt.encode(
        claims, settings.JWT_SECRET.get_secret_value(), algorithm=settings.JWT_ALGORITHM
    )


def encode_access(user_id: uuid.UUID, role, sid: str, *, settings) -> str:
    now = datetime.now(UTC)
    return _encode(
        {
            "sub": str(user_id),
            "role": getattr(role, "value", role),
            "jti": str(uuid.uuid4()),
            "sid": sid,
            "typ": "access",
            "iat": now,
            "exp": now + timedelta(minutes=settings.ACCESS_TOKEN_TTL_MIN),
        },
        settings=settings,
    )


def encode_refresh(user_id: uuid.UUID, role, *, settings) -> tuple[str, str, datetime]:
    now = datetime.now(UTC)
    jti = str(uuid.uuid4())
    expires_at = now + timedelta(days=settings.REFRESH_TOKEN_TTL_DAYS)
    token = _encode(
        {
            "sub": str(user_id),
            "role": getattr(role, "value", role),
            "jti": jti,
            "typ": "refresh",
            "iat": now,
            "exp": expires_at,
        },
        settings=settings,
    )
    return token, jti, expires_at


def _reset_key(hashed_password: str, *, settings) -> str:
    """The reset token's signing key is the app secret PLUS the user's current password hash.

    That is what makes the link single-use with no table to store, prune or race on: the moment the
    password changes, the hash changes, the key changes, and every link ever issued for that account
    stops verifying. Logging out everywhere on reset is handled separately, in the service.
    """
    return settings.JWT_SECRET.get_secret_value() + hashed_password


def encode_reset(user_id: uuid.UUID, hashed_password: str, *, settings) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": str(user_id),
            "typ": "reset",
            "iat": now,
            "exp": now + timedelta(minutes=settings.RESET_TOKEN_TTL_MIN),
        },
        _reset_key(hashed_password, settings=settings),
        algorithm=settings.JWT_ALGORITHM,
    )


def reset_subject(token: str) -> uuid.UUID:
    """The user id, read WITHOUT verifying — the signing key depends on the user's hash, so the
    row has to be loaded before the signature can be checked. Nothing is trusted until
    `decode_reset` runs."""
    try:
        claims = jwt.decode(token, options={"verify_signature": False})
        return uuid.UUID(claims["sub"])
    except (jwt.PyJWTError, KeyError, ValueError, TypeError) as exc:
        raise AuthError(400, RESET_INVALID, reason="bad_reset_token") from exc


def decode_reset(token: str, hashed_password: str, *, settings) -> dict:
    try:
        claims = jwt.decode(
            token,
            _reset_key(hashed_password, settings=settings),
            algorithms=[settings.JWT_ALGORITHM],
            leeway=settings.JWT_LEEWAY_S,
        )
    except jwt.PyJWTError as exc:
        # Expired, tampered with, or already used (the hash it was signed against is gone) — the
        # user can only do one thing about any of them, so they get one message.
        raise AuthError(400, RESET_INVALID, reason="bad_reset_token") from exc

    if claims.get("typ") != "reset":
        raise AuthError(400, RESET_INVALID, reason="wrong_typ")
    return claims


def decode_token(token: str, *, expect: Literal["access", "refresh"], settings) -> dict:
    try:
        claims = jwt.decode(
            token,
            settings.JWT_SECRET.get_secret_value(),
            algorithms=[settings.JWT_ALGORITHM],
            leeway=settings.JWT_LEEWAY_S,
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthError(401, GENERIC, reason="expired") from exc
    except jwt.PyJWTError as exc:
        raise AuthError(401, GENERIC, reason="bad_token") from exc

    if claims.get("typ") != expect:
        raise AuthError(401, GENERIC, reason="wrong_typ")
    return claims
