from typing import Literal

from fastapi import Depends, Request
from fastapi.security import APIKeyHeader, OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.schemas import Principal
from app.auth.service import resolve_api_key, resolve_jwt
from app.core.exceptions import FORBIDDEN, GENERIC, AuthError
from app.core.security import decode_token
from app.core.settings import settings
from app.db.session import get_session

_bearer = OAuth2PasswordBearer(tokenUrl="api/auth/token", auto_error=False)
_api_key = APIKeyHeader(name="X-API-Key", auto_error=False)


async def access_claims(token: str | None = Depends(_bearer)) -> dict:
    if not token:
        raise AuthError(401, GENERIC, reason="no_credentials")
    return decode_token(token, expect="access", settings=settings)


async def get_current_user_optional(
    token: str | None = Depends(_bearer),
    key: str | None = Depends(_api_key),
    session: AsyncSession = Depends(get_session),
) -> Principal | None:
    if token:
        return await resolve_jwt(session, token, settings=settings)
    if key:
        return await resolve_api_key(session, key, settings=settings)
    return None


async def get_current_user(
    principal: Principal | None = Depends(get_current_user_optional),
) -> Principal:
    if principal is None:
        raise AuthError(401, GENERIC, reason="no_credentials")
    return principal


def require_role(role: Literal["admin"]):
    async def _dep(principal: Principal = Depends(get_current_user)) -> Principal:
        if principal.kind != role:
            raise AuthError(403, FORBIDDEN, reason="forbidden")
        return principal

    return _dep


def rate_tier(principal: Principal | None, ip: str) -> tuple[str, int]:
    if principal is None:
        return f"ip:{ip}", settings.RATE_LIMIT_ANON_PER_MIN
    if principal.kind == "api_key":
        return f"apikey:{principal.api_key_id}", settings.RATE_LIMIT_API_KEY_PER_MIN
    if principal.kind == "admin":
        return f"user:{principal.user_id}", settings.RATE_LIMIT_ADMIN_PER_MIN
    return f"user:{principal.user_id}", settings.RATE_LIMIT_STUDENT_PER_MIN


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"
