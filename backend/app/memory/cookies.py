"""Signed anonymous-session cookie (AC-1/AC-8). Reuses the app's pyjwt surface + `JWT_SECRET` — no
new dependency (design §4). A tampered / wrong-secret / wrong-`typ` cookie verifies to `None`, so a
forged cookie is simply treated as "no session"."""

import uuid

import jwt

COOKIE_NAME = "anon_session"
_TYP = "anon_session"


def sign(session_id: uuid.UUID, *, settings) -> str:
    return jwt.encode(
        {"sid": str(session_id), "typ": _TYP},
        settings.JWT_SECRET.get_secret_value(),
        algorithm=settings.JWT_ALGORITHM,
    )


def verify(cookie: str | None, *, settings) -> uuid.UUID | None:
    if not cookie:
        return None
    try:
        claims = jwt.decode(
            cookie,
            settings.JWT_SECRET.get_secret_value(),
            algorithms=[settings.JWT_ALGORITHM],
        )
    except jwt.PyJWTError:
        return None
    if claims.get("typ") != _TYP:
        return None
    try:
        return uuid.UUID(claims["sid"])
    except (KeyError, ValueError):
        return None
