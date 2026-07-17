"""The F10 feature card's four acceptance criteria — the definition of done (requirements §4).

  1. Flow:        register -> token -> authed -> refresh -> logout -> old refresh rejected
  2. Roles:       student blocked from /internal/*; admin passes; API key limited to ask
  3. Swagger:     the Authorize password grant works end to end
  4. Unit-tested: bcrypt + blacklist + lockout

There is no eval gate here and that is deliberate: CLAUDE.md's label sequence runs
`f9-cache-after -> f17-memory-after` with no f10 slot, and auth touches no retrieval, so a delta
report would be a table of zeros.
"""

import asyncio
import time

import pytest

from app.auth import service
from app.auth.schemas import RegisterRequest
from app.core.exceptions import AuthError
from app.core.security import hash_password
from app.db.enums import UserRole

from .conftest import make_settings

PW = "probation123"
EMAIL = "s@pu.edu.pk"


async def test_ac1_full_flow(client):
    """register -> token -> authed request -> refresh -> logout -> old refresh rejected."""
    r = await client.post("/api/auth/register", json={"email": EMAIL, "password": PW})
    assert r.status_code == 201

    pair = (
        await client.post("/api/auth/token", data={"username": EMAIL, "password": PW})
    ).json()
    assert pair["token_type"] == "bearer"

    authed = await client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {pair['access_token']}"}
    )
    assert authed.status_code == 200 and authed.json()["email"] == EMAIL

    rotated = await client.post(
        "/api/auth/refresh", json={"refresh_token": pair["refresh_token"]}
    )
    assert rotated.status_code == 200
    new_pair = rotated.json()

    # the rotated-out refresh token is dead the moment it is replaced
    assert (
        await client.post("/api/auth/refresh", json={"refresh_token": pair["refresh_token"]})
    ).status_code == 401

    logout = await client.post(
        "/api/auth/logout", headers={"Authorization": f"Bearer {new_pair['access_token']}"}
    )
    assert logout.status_code == 204

    assert (
        await client.post("/api/auth/refresh", json={"refresh_token": new_pair["refresh_token"]})
    ).status_code == 401
    assert (
        await client.get(
            "/api/auth/me", headers={"Authorization": f"Bearer {new_pair['access_token']}"}
        )
    ).status_code == 401


async def test_ac2_roles(client, sessionmaker_, auth_settings):
    """student blocked from /internal/*; admin passes; API key limited to ask."""
    async with sessionmaker_() as s:
        await service.register(
            s, RegisterRequest(email=EMAIL, password=PW), settings=auth_settings
        )
        admin = await service.register(
            s, RegisterRequest(email="admin@pu.edu.pk", password=PW), settings=auth_settings
        )
        admin.role = UserRole.admin
        await s.commit()
        raw_key = await service.issue_key(s, EMAIL, "telegram-bot")

    student = (
        await client.post("/api/auth/token", data={"username": EMAIL, "password": PW})
    ).json()["access_token"]
    admin_token = (
        await client.post(
            "/api/auth/token", data={"username": "admin@pu.edu.pk", "password": PW}
        )
    ).json()["access_token"]

    assert (
        await client.get("/internal/ping", headers={"Authorization": f"Bearer {student}"})
    ).status_code == 403
    assert (
        await client.get("/internal/ping", headers={"Authorization": f"Bearer {admin_token}"})
    ).status_code == 200
    assert (
        await client.get("/internal/ping", headers={"X-API-Key": raw_key})
    ).status_code == 403
    assert (await client.get("/internal/ping")).status_code == 401

    # ask-only: the key authenticates, it just cannot reach the admin surface
    assert (await client.get("/api/auth/me", headers={"X-API-Key": raw_key})).status_code == 200


async def test_ac3_swagger_authorize_grant(client):
    """What the Authorize button does: an x-www-form-urlencoded password grant against the
    advertised tokenUrl, then a bearer call. The button click itself is manual — see DONE.md."""
    await client.post("/api/auth/register", json={"email": EMAIL, "password": PW})

    token_url = (await client.get("/openapi.json")).json()["components"]["securitySchemes"][
        "OAuth2PasswordBearer"
    ]["flows"]["password"]["tokenUrl"]

    grant = await client.post(
        f"/{token_url}",
        data={"grant_type": "password", "username": EMAIL, "password": PW},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert grant.status_code == 200

    me = await client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {grant.json()['access_token']}"}
    )
    assert me.status_code == 200


async def test_ac4_bcrypt_does_not_serialize_the_event_loop():
    """AC-17: bcrypt is deliberately slow CPU work, so a burst of logins must not freeze the loop
    every other request shares."""
    settings = make_settings(BCRYPT_ROUNDS=10)

    t0 = time.perf_counter()
    await hash_password("warm-the-thread-pool", settings=settings)
    single = time.perf_counter() - t0

    t0 = time.perf_counter()
    await asyncio.gather(*(hash_password(f"pw-{i}", settings=settings) for i in range(8)))
    concurrent = time.perf_counter() - t0

    assert concurrent < single * 8 * 0.6, (
        f"8 concurrent hashes took {concurrent:.2f}s vs {single:.2f}s for one — "
        "bcrypt appears to be running on the event loop"
    )


async def test_ac4_blacklist(session, auth_settings):
    """refresh_tokens IS the blacklist: revoked_at flips and the access token dies with it."""
    await service.register(session, RegisterRequest(email=EMAIL, password=PW), settings=auth_settings)
    access, refresh = await service.authenticate(session, EMAIL, PW, settings=auth_settings)
    from app.core.security import decode_token

    sid = decode_token(access, expect="access", settings=auth_settings)["sid"]
    assert await service.resolve_jwt(session, access, settings=auth_settings)

    await service.revoke_family(session, sid)

    with pytest.raises(AuthError):
        await service.resolve_jwt(session, access, settings=auth_settings)
    with pytest.raises(AuthError):
        await service.rotate_refresh(session, refresh, settings=auth_settings)


async def test_ac4_lockout(session, auth_settings):
    await service.register(session, RegisterRequest(email=EMAIL, password=PW), settings=auth_settings)

    for _ in range(auth_settings.LOGIN_MAX_FAILURES):
        with pytest.raises(AuthError):
            await service.authenticate(session, EMAIL, "wrongpassword", settings=auth_settings)

    with pytest.raises(AuthError) as exc:
        await service.authenticate(session, EMAIL, PW, settings=auth_settings)
    assert exc.value.status == 429
