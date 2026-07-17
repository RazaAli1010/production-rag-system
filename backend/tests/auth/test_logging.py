"""AC-29: no token, key, password, or hash may reach a log line. Asserted against a captured
structlog stream over the full flow, because "we just don't log secrets" is a claim that rots.
"""

import pytest
import structlog
from sqlalchemy import select

from app.db.models import User

PW = "probation123"
EMAIL = "s@pu.edu.pk"


@pytest.fixture
def logs():
    structlog.configure(processors=[structlog.testing.LogCapture()])
    cap = structlog.testing.LogCapture()
    structlog.configure(processors=[cap])
    yield cap
    structlog.reset_defaults()


def _blob(entries) -> str:
    return " ".join(repr(e) for e in entries)


async def test_no_secret_reaches_the_logs_across_the_whole_flow(client, logs, session):
    await client.post("/api/auth/register", json={"email": EMAIL, "password": PW})
    pair = (
        await client.post("/api/auth/token", data={"username": EMAIL, "password": PW})
    ).json()
    headers = {"Authorization": f"Bearer {pair['access_token']}"}
    await client.get("/api/auth/me", headers=headers)
    rotated = (
        await client.post("/api/auth/refresh", json={"refresh_token": pair["refresh_token"]})
    ).json()
    await client.post("/api/auth/logout", headers={"Authorization": f"Bearer {rotated['access_token']}"})
    await client.post("/api/auth/token", data={"username": EMAIL, "password": "wrongpassword"})

    hashed = (await session.scalar(select(User).where(User.email == EMAIL))).hashed_password
    blob = _blob(logs.entries)

    assert logs.entries, "expected auth events to be logged at all"
    for secret in [
        PW,
        pair["access_token"],
        pair["refresh_token"],
        rotated["access_token"],
        rotated["refresh_token"],
        hashed,
    ]:
        assert secret not in blob


async def test_rejection_reason_is_logged_but_not_returned(client, logs):
    r = await client.get("/api/auth/me", headers={"Authorization": "Bearer not.a.token"})

    assert r.json()["detail"] == "Could not validate credentials"
    rejects = [e for e in logs.entries if e.get("event") == "auth.reject"]
    assert rejects and rejects[-1]["reason"] == "bad_token"


async def test_api_key_never_reaches_the_logs(client, sessionmaker_, auth_settings, logs):
    from app.auth import service
    from app.auth.schemas import RegisterRequest

    async with sessionmaker_() as s:
        await service.register(
            s, RegisterRequest(email=EMAIL, password=PW), settings=auth_settings
        )
        raw = await service.issue_key(s, EMAIL, "telegram")

    await client.get("/api/auth/me", headers={"X-API-Key": raw})
    await client.get("/internal/ping", headers={"X-API-Key": raw})

    assert raw not in _blob(logs.entries)
