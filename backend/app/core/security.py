import functools
import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

import anyio
import jwt
from passlib.context import CryptContext

from app.core.exceptions import GENERIC, AuthError

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
