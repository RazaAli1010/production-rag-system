"""The §3.6 authorization matrix as a table test (AC-36).

| Actor     | /api/ask | rate tier        | history | /internal/* |
|-----------|----------|------------------|---------|-------------|
| Anonymous | yes      | 5/min per IP     | no      | no          |
| Student   | yes      | 20/min per user  | own     | no          |
| Admin     | yes      | 60/min per user  | own     | yes         |
| API key   | yes      | 30/min per key   | no      | yes -> NO   |
"""

import pytest
from sqlalchemy import select

from app.auth import service
from app.auth.deps import rate_tier
from app.auth.schemas import RegisterRequest
from app.db.enums import UserRole
from app.db.models import User

PW = "probation123"

ACTORS = ["anonymous", "student", "admin", "api_key"]


@pytest.fixture
async def actors(client, sessionmaker_, auth_settings):
    """Builds one live credential per actor kind against the same app the client drives."""
    out = {"anonymous": {}}

    async with sessionmaker_() as s:
        await service.register(
            s, RegisterRequest(email="student@pu.edu.pk", password=PW), settings=auth_settings
        )
        admin = await service.register(
            s, RegisterRequest(email="admin@pu.edu.pk", password=PW), settings=auth_settings
        )
        admin.role = UserRole.admin
        await s.commit()
        raw_key = await service.issue_key(s, "student@pu.edu.pk", "telegram")

    for name, email in [("student", "student@pu.edu.pk"), ("admin", "admin@pu.edu.pk")]:
        pair = (
            await client.post("/api/auth/token", data={"username": email, "password": PW})
        ).json()
        out[name] = {"Authorization": f"Bearer {pair['access_token']}"}

    out["api_key"] = {"X-API-Key": raw_key}
    return out


@pytest.mark.parametrize(
    "actor, expected",
    [("anonymous", 401), ("student", 403), ("admin", 200), ("api_key", 403)],
)
async def test_internal_access(client, actors, actor, expected):
    r = await client.get("/internal/ping", headers=actors[actor])

    assert r.status_code == expected


@pytest.mark.parametrize(
    "actor, expected",
    [("anonymous", 401), ("student", 200), ("admin", 200), ("api_key", 200)],
)
async def test_me_access(client, actors, actor, expected):
    """/me stands in for the authed student surface: every non-anonymous actor resolves."""
    r = await client.get("/api/auth/me", headers=actors[actor])

    assert r.status_code == expected


@pytest.mark.parametrize(
    "actor, expected_limit",
    [("anonymous", 5), ("student", 20), ("admin", 60), ("api_key", 30)],
)
async def test_rate_tiers(client, actors, sessionmaker_, auth_settings, actor, expected_limit):
    from app.auth.deps import get_current_user_optional

    headers = actors[actor]
    async with sessionmaker_() as s:
        principal = await get_current_user_optional(
            token=(headers.get("Authorization") or " ").split(" ")[-1].strip() or None,
            key=headers.get("X-API-Key"),
            session=s,
        )

    assert rate_tier(principal, "1.2.3.4")[1] == expected_limit


@pytest.mark.parametrize(
    "actor, has_history",
    [("anonymous", False), ("student", True), ("admin", True), ("api_key", False)],
)
async def test_history_eligibility(client, actors, sessionmaker_, auth_settings, actor, has_history):
    """History (F17 sessions/messages) hangs off a user_id, which only a JWT principal supplies.
    F10's contribution is the principal, so that is what this asserts."""
    from app.auth.deps import get_current_user_optional

    headers = actors[actor]
    async with sessionmaker_() as s:
        principal = await get_current_user_optional(
            token=(headers.get("Authorization") or " ").split(" ")[-1].strip() or None,
            key=headers.get("X-API-Key"),
            session=s,
        )

    eligible = principal is not None and principal.kind in ("student", "admin")
    assert eligible is has_history


async def test_api_key_owner_is_a_real_user(client, actors, session):
    """The key resolves to its owning user, which is what makes the ask-only scope a property of
    `kind` rather than of the user's role."""
    r = await client.get("/api/auth/me", headers=actors["api_key"])

    assert r.status_code == 200
    assert r.json()["email"] == "student@pu.edu.pk"
    assert await session.scalar(select(User).where(User.email == "student@pu.edu.pk"))
