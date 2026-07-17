import pytest

PW = "probation123"
EMAIL = "s@pu.edu.pk"


async def register(client, email=EMAIL, password=PW):
    return await client.post("/api/auth/register", json={"email": email, "password": password})


async def login(client, email=EMAIL, password=PW):
    return await client.post(
        "/api/auth/token", data={"username": email, "password": password}
    )


async def test_register_returns_201_and_no_hash(client):
    r = await register(client)

    assert r.status_code == 201
    body = r.json()
    assert body["email"] == EMAIL
    assert body["role"] == "student"
    assert "hashed_password" not in body
    assert "$2b$" not in r.text


async def test_duplicate_registration_is_409(client):
    await register(client)

    r = await register(client)

    assert r.status_code == 409


@pytest.mark.parametrize(
    "payload",
    [
        {"email": EMAIL, "password": "short"},
        {"email": "not-an-email", "password": PW},
        {"email": EMAIL, "password": PW, "role": "admin"},
    ],
    ids=["short_password", "bad_email", "role_injection"],
)
async def test_register_validation_is_422(client, payload):
    r = await client.post("/api/auth/register", json=payload)

    assert r.status_code == 422


async def test_token_returns_a_bearer_pair(client):
    await register(client)

    r = await login(client)

    assert r.status_code == 200
    body = r.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"] and body["refresh_token"]


async def test_token_with_bad_password_is_401_with_www_authenticate(client):
    await register(client)

    r = await login(client, password="wrongpassword")

    assert r.status_code == 401
    assert r.json()["detail"] == "Incorrect email or password"
    assert r.headers["www-authenticate"] == "Bearer"


async def test_unknown_email_is_indistinguishable_from_a_wrong_password(client):
    await register(client)

    wrong = await login(client, password="wrongpassword")
    unknown = await login(client, email="nobody@pu.edu.pk")

    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()


async def test_me_returns_the_profile(client):
    await register(client)
    access = (await login(client)).json()["access_token"]

    r = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {access}"})

    assert r.status_code == 200
    assert r.json()["email"] == EMAIL
    assert "hashed_password" not in r.text


async def test_me_without_credentials_is_401(client):
    r = await client.get("/api/auth/me")

    assert r.status_code == 401
    assert r.json()["detail"] == "Could not validate credentials"


async def test_me_with_a_garbage_token_is_401(client):
    r = await client.get("/api/auth/me", headers={"Authorization": "Bearer not.a.token"})

    assert r.status_code == 401


async def test_refresh_rotates_the_pair(client):
    await register(client)
    pair = (await login(client)).json()

    r = await client.post("/api/auth/refresh", json={"refresh_token": pair["refresh_token"]})

    assert r.status_code == 200
    assert r.json()["refresh_token"] != pair["refresh_token"]


async def test_refresh_rejects_an_access_token(client):
    await register(client)
    pair = (await login(client)).json()

    r = await client.post("/api/auth/refresh", json={"refresh_token": pair["access_token"]})

    assert r.status_code == 401


async def test_logout_is_204_and_idempotent(client, session):
    """AC-24: a repeat logout is a no-op 204 and must not overwrite the original revoked_at."""
    from sqlalchemy import select

    from app.db.models import RefreshToken

    await register(client)
    access = (await login(client)).json()["access_token"]
    headers = {"Authorization": f"Bearer {access}"}

    first = await client.post("/api/auth/logout", headers=headers)
    revoked_at = (await session.scalar(select(RefreshToken))).revoked_at
    second = await client.post("/api/auth/logout", headers=headers)

    assert first.status_code == second.status_code == 204
    assert (await session.scalar(select(RefreshToken))).revoked_at == revoked_at


async def test_logout_kills_the_access_token_immediately(client):
    """AC-23: the sid claim earning its keep — no waiting out the 15min expiry."""
    await register(client)
    access = (await login(client)).json()["access_token"]
    headers = {"Authorization": f"Bearer {access}"}
    assert (await client.get("/api/auth/me", headers=headers)).status_code == 200

    await client.post("/api/auth/logout", headers=headers)

    assert (await client.get("/api/auth/me", headers=headers)).status_code == 401


async def test_logout_rejects_the_old_refresh_token(client):
    await register(client)
    pair = (await login(client)).json()
    headers = {"Authorization": f"Bearer {pair['access_token']}"}

    await client.post("/api/auth/logout", headers=headers)

    r = await client.post("/api/auth/refresh", json={"refresh_token": pair["refresh_token"]})
    assert r.status_code == 401


async def test_logout_without_credentials_is_401(client):
    r = await client.post("/api/auth/logout")

    assert r.status_code == 401
