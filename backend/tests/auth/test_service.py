import asyncio

import pytest
from sqlalchemy import select

from app.auth import service
from app.auth.schemas import RegisterRequest
from app.core.exceptions import AuthError
from app.core.security import decode_token, encode_access, new_api_key
from app.db.enums import UserRole
from app.db.models import ApiKey, LoginAttempt, RefreshToken, User

from .conftest import make_settings

PW = "probation123"


async def _register(session, settings, email="s@pu.edu.pk") -> User:
    return await service.register(
        session, RegisterRequest(email=email, password=PW), settings=settings
    )


# --------------------------------------------------------------------------- register


async def test_register_creates_a_student(session, auth_settings):
    user = await _register(session, auth_settings)

    assert user.role is UserRole.student
    assert user.is_active is True
    assert user.hashed_password != PW


async def test_duplicate_registration_is_409(session, auth_settings):
    await _register(session, auth_settings)

    with pytest.raises(AuthError) as exc:
        await _register(session, auth_settings)
    assert exc.value.status == 409


@pytest.mark.parametrize(
    "email, allowed",
    [("s@pu.edu.pk", True), ("s@gmail.com", False)],
)
async def test_domain_allowlist_when_configured(session, email, allowed):
    settings = make_settings(BCRYPT_ROUNDS=4, AUTH_EMAIL_DOMAIN_ALLOWLIST=["edu.pk"])

    if allowed:
        assert await _register(session, settings, email=email)
    else:
        with pytest.raises(AuthError) as exc:
            await _register(session, settings, email=email)
        assert exc.value.status == 403


async def test_allowlist_empty_by_default_accepts_any_domain(session, auth_settings):
    assert await _register(session, auth_settings, email="s@gmail.com")


# --------------------------------------------------------------------------- authenticate


async def test_authenticate_issues_a_pair_whose_sid_matches_the_refresh_row(session, auth_settings):
    user = await _register(session, auth_settings)

    access, refresh = await service.authenticate(
        session, "s@pu.edu.pk", PW, settings=auth_settings
    )

    access_claims = decode_token(access, expect="access", settings=auth_settings)
    refresh_claims = decode_token(refresh, expect="refresh", settings=auth_settings)
    row = await session.scalar(select(RefreshToken).where(RefreshToken.user_id == user.id))

    assert access_claims["sid"] == refresh_claims["jti"] == row.jti
    assert row.revoked_at is None


@pytest.mark.parametrize(
    "email, password, setup",
    [
        ("s@pu.edu.pk", "wrongpassword", "exists"),
        ("nobody@pu.edu.pk", PW, "unknown"),
        ("s@pu.edu.pk", PW, "inactive"),
    ],
    ids=["wrong_password", "unknown_email", "inactive_user"],
)
async def test_every_login_failure_is_the_same_401(session, auth_settings, email, password, setup):
    user = await _register(session, auth_settings)
    if setup == "inactive":
        user.is_active = False
        await session.commit()

    with pytest.raises(AuthError) as exc:
        await service.authenticate(session, email, password, settings=auth_settings)

    assert exc.value.status == 401
    assert exc.value.detail == "Incorrect email or password"


async def test_login_attempts_recorded_on_both_paths(session, auth_settings):
    await _register(session, auth_settings)

    with pytest.raises(AuthError):
        await service.authenticate(session, "s@pu.edu.pk", "wrongpassword", settings=auth_settings)
    await service.authenticate(session, "s@pu.edu.pk", PW, settings=auth_settings)

    attempts = (
        await session.execute(select(LoginAttempt).order_by(LoginAttempt.attempted_at))
    ).scalars().all()

    assert [a.success for a in attempts] == [False, True]
    assert {a.email_or_ip for a in attempts} == {"s@pu.edu.pk"}


# --------------------------------------------------------------------------- rotate / revoke


async def test_rotate_revokes_the_old_row_and_chains_it(session, auth_settings):
    await _register(session, auth_settings)
    _, refresh = await service.authenticate(session, "s@pu.edu.pk", PW, settings=auth_settings)
    old_jti = decode_token(refresh, expect="refresh", settings=auth_settings)["jti"]

    access2, refresh2 = await service.rotate_refresh(session, refresh, settings=auth_settings)

    new_jti = decode_token(refresh2, expect="refresh", settings=auth_settings)["jti"]
    old = await session.scalar(select(RefreshToken).where(RefreshToken.jti == old_jti))
    assert old.revoked_at is not None
    assert old.replaced_by_jti == new_jti
    assert decode_token(access2, expect="access", settings=auth_settings)["sid"] == new_jti


async def test_reusing_a_rotated_refresh_is_401(session, auth_settings):
    await _register(session, auth_settings)
    _, refresh = await service.authenticate(session, "s@pu.edu.pk", PW, settings=auth_settings)
    await service.rotate_refresh(session, refresh, settings=auth_settings)

    with pytest.raises(AuthError) as exc:
        await service.rotate_refresh(session, refresh, settings=auth_settings)
    assert exc.value.status == 401


async def test_an_access_token_cannot_be_rotated(session, auth_settings):
    await _register(session, auth_settings)
    access, _ = await service.authenticate(session, "s@pu.edu.pk", PW, settings=auth_settings)

    with pytest.raises(AuthError) as exc:
        await service.rotate_refresh(session, access, settings=auth_settings)
    assert exc.value.reason == "wrong_typ"


async def test_concurrent_rotation_yields_exactly_one_winner(sessionmaker_, auth_settings):
    """The FOR UPDATE proof: without it both callers read the token unrevoked and mint two
    valid families."""
    async with sessionmaker_() as s:
        await _register(s, auth_settings)
        _, refresh = await service.authenticate(s, "s@pu.edu.pk", PW, settings=auth_settings)

    async def rotate():
        async with sessionmaker_() as s:
            return await service.rotate_refresh(s, refresh, settings=auth_settings)

    results = await asyncio.gather(rotate(), rotate(), return_exceptions=True)

    winners = [r for r in results if not isinstance(r, Exception)]
    losers = [r for r in results if isinstance(r, AuthError)]
    assert len(winners) == 1
    assert len(losers) == 1 and losers[0].status == 401


async def test_revoke_family_is_idempotent(session, auth_settings):
    await _register(session, auth_settings)
    _, refresh = await service.authenticate(session, "s@pu.edu.pk", PW, settings=auth_settings)
    jti = decode_token(refresh, expect="refresh", settings=auth_settings)["jti"]

    await service.revoke_family(session, jti)
    first = (await session.scalar(select(RefreshToken).where(RefreshToken.jti == jti))).revoked_at
    await service.revoke_family(session, jti)
    second = (await session.scalar(select(RefreshToken).where(RefreshToken.jti == jti))).revoked_at

    assert first == second


# --------------------------------------------------------------------------- resolve


async def test_resolve_jwt_returns_a_principal(session, auth_settings):
    user = await _register(session, auth_settings)
    access, _ = await service.authenticate(session, "s@pu.edu.pk", PW, settings=auth_settings)

    p = await service.resolve_jwt(session, access, settings=auth_settings)

    assert p.kind == "student"
    assert p.user_id == user.id
    assert p.email == "s@pu.edu.pk"


async def test_logout_kills_the_access_token_immediately(session, auth_settings):
    """AC-23: the whole reason access tokens carry `sid`."""
    await _register(session, auth_settings)
    access, refresh = await service.authenticate(session, "s@pu.edu.pk", PW, settings=auth_settings)
    sid = decode_token(access, expect="access", settings=auth_settings)["sid"]
    assert await service.resolve_jwt(session, access, settings=auth_settings)

    await service.revoke_family(session, sid)

    with pytest.raises(AuthError) as exc:
        await service.resolve_jwt(session, access, settings=auth_settings)
    assert exc.value.reason == "revoked_family"


async def test_role_comes_from_the_row_not_the_claim(session, auth_settings):
    user = await _register(session, auth_settings)
    user.role = UserRole.admin
    await session.commit()
    access, _ = await service.authenticate(session, "s@pu.edu.pk", PW, settings=auth_settings)
    assert decode_token(access, expect="access", settings=auth_settings)["role"] == "admin"

    user.role = UserRole.student
    await session.commit()

    p = await service.resolve_jwt(session, access, settings=auth_settings)
    assert p.kind == "student"
    assert p.role is UserRole.student


async def test_resolve_jwt_rejects_an_inactive_user(session, auth_settings):
    user = await _register(session, auth_settings)
    access, _ = await service.authenticate(session, "s@pu.edu.pk", PW, settings=auth_settings)
    user.is_active = False
    await session.commit()

    with pytest.raises(AuthError):
        await service.resolve_jwt(session, access, settings=auth_settings)


async def test_resolve_jwt_rejects_a_token_with_an_unknown_sid(session, auth_settings):
    user = await _register(session, auth_settings)
    forged = encode_access(user.id, UserRole.admin, sid="never-issued", settings=auth_settings)

    with pytest.raises(AuthError):
        await service.resolve_jwt(session, forged, settings=auth_settings)


async def test_resolve_api_key(session, auth_settings):
    user = await _register(session, auth_settings)
    raw = await service.issue_key(session, "s@pu.edu.pk", "telegram")

    p = await service.resolve_api_key(session, raw, settings=auth_settings)

    assert p.kind == "api_key"
    assert p.user_id == user.id
    assert p.api_key_id is not None


async def test_an_admins_api_key_is_still_only_kind_api_key(session, auth_settings):
    user = await _register(session, auth_settings)
    user.role = UserRole.admin
    await session.commit()
    raw = await service.issue_key(session, "s@pu.edu.pk", "bot")

    p = await service.resolve_api_key(session, raw, settings=auth_settings)

    assert p.kind == "api_key"
    assert p.is_admin is False


async def test_revoked_api_key_is_401(session, auth_settings):
    await _register(session, auth_settings)
    raw = await service.issue_key(session, "s@pu.edu.pk", "telegram")
    await service.revoke_key(session, "telegram")

    with pytest.raises(AuthError):
        await service.resolve_api_key(session, raw, settings=auth_settings)


async def test_unknown_api_key_is_401(session, auth_settings):
    raw, _ = new_api_key()

    with pytest.raises(AuthError):
        await service.resolve_api_key(session, raw, settings=auth_settings)


async def test_issue_key_stores_only_the_hash(session, auth_settings):
    await _register(session, auth_settings)

    raw = await service.issue_key(session, "s@pu.edu.pk", "telegram")

    key = await session.scalar(select(ApiKey))
    assert key.key_hash != raw
    assert raw not in key.key_hash
