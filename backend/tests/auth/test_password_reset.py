"""Password reset: the link works once, dies on use, and the endpoint never leaks who exists."""

import pytest
from sqlalchemy import select

from app.auth import service
from app.core.security import encode_reset
from app.db.models import RefreshToken, User

PW = "Probation123"
NEW_PW = "Newpassword1"
EMAIL = "s@pu.edu.pk"


@pytest.fixture(autouse=True)
def captured_links(monkeypatch):
    """No network in tests: capture what the mailer was handed instead of sending it."""
    links: list[tuple[str, str]] = []

    async def _capture(to, link, *, settings):
        links.append((to, link))

    monkeypatch.setattr(service, "send_reset_link", _capture)
    return links


async def register(client, email=EMAIL, password=PW):
    return await client.post("/api/auth/register", json={"email": email, "password": password})


async def login(client, email=EMAIL, password=PW):
    return await client.post("/api/auth/token", data={"username": email, "password": password})


async def forgot(client, email=EMAIL):
    return await client.post("/api/auth/forgot-password", json={"email": email})


def token_of(links) -> str:
    return links[-1][1].split("token=")[1]


async def test_forgot_password_emails_a_link(client, captured_links):
    await register(client)

    r = await forgot(client)

    assert r.status_code == 202
    assert captured_links[0][0] == EMAIL
    assert "/reset-password?token=" in captured_links[0][1]


async def test_unknown_email_is_indistinguishable_and_sends_nothing(client, captured_links):
    await register(client)

    known = await forgot(client)
    unknown = await forgot(client, "nobody@pu.edu.pk")

    assert known.status_code == unknown.status_code == 202
    assert known.content == unknown.content
    assert [to for to, _ in captured_links] == [EMAIL]  # no mail to a non-existent address


async def test_reset_sets_the_new_password_and_kills_the_old_one(client, captured_links):
    await register(client)
    await forgot(client)

    r = await client.post(
        "/api/auth/reset-password", json={"token": token_of(captured_links), "password": NEW_PW}
    )

    assert r.status_code == 204
    assert (await login(client, password=NEW_PW)).status_code == 200
    assert (await login(client, password=PW)).status_code == 401


async def test_a_link_cannot_be_used_twice(client, captured_links):
    """The token is signed with the password hash, so the first reset invalidates it."""
    await register(client)
    await forgot(client)
    token = token_of(captured_links)
    body = {"token": token, "password": NEW_PW}
    assert (await client.post("/api/auth/reset-password", json=body)).status_code == 204

    r = await client.post(
        "/api/auth/reset-password", json={"token": token, "password": "Other12345"}
    )

    assert r.status_code == 400
    assert "invalid or has expired" in r.json()["error"]["message"]


async def test_reset_revokes_every_live_session(client, session, captured_links):
    await register(client)
    access = (await login(client)).json()["access_token"]
    await forgot(client)

    await client.post(
        "/api/auth/reset-password", json={"token": token_of(captured_links), "password": NEW_PW}
    )

    assert (
        await client.get("/api/auth/me", headers={"Authorization": f"Bearer {access}"})
    ).status_code == 401
    live = await session.scalars(
        select(RefreshToken).where(RefreshToken.revoked_at.is_(None))
    )
    assert live.all() == []


async def test_a_forged_token_is_rejected(client, session, auth_settings, captured_links):
    await register(client)
    user = await session.scalar(select(User).where(User.email == EMAIL))
    forged = encode_reset(user.id, "not-the-real-hash", settings=auth_settings)

    r = await client.post("/api/auth/reset-password", json={"token": forged, "password": NEW_PW})

    assert r.status_code == 400


@pytest.mark.parametrize("token", ["", "garbage", "a.b.c"], ids=["empty", "garbage", "jwt_shaped"])
async def test_a_malformed_token_is_a_400_not_a_500(client, token):
    r = await client.post("/api/auth/reset-password", json={"token": token, "password": NEW_PW})

    assert r.status_code == 400


async def test_reset_enforces_the_password_policy(client, captured_links):
    await register(client)
    await forgot(client)

    r = await client.post(
        "/api/auth/reset-password", json={"token": token_of(captured_links), "password": "weakpass"}
    )

    assert r.status_code == 422
    assert "uppercase" in r.json()["error"]["detail"][0]["msg"]


async def test_repeated_requests_are_throttled(client, captured_links):
    """Anti mail-bomb: the endpoint keeps answering 202, it just stops sending."""
    await register(client)

    for _ in range(5):
        assert (await forgot(client)).status_code == 202

    assert len(captured_links) == 3  # RESET_MAX_PER_WINDOW


async def test_reset_throttle_does_not_lock_out_login(client, captured_links):
    """The throttle shares login_attempts; keyed apart so it cannot become a lockout weapon."""
    await register(client)

    for _ in range(15):
        await forgot(client)

    assert (await login(client)).status_code == 200
