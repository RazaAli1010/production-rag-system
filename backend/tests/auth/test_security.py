import time
import uuid

import jwt
import pytest

from app.core.exceptions import GENERIC, AuthError
from app.core.security import (
    _dummy_hash,
    api_key_hash,
    decode_token,
    encode_access,
    encode_refresh,
    hash_password,
    new_api_key,
    verify_password,
)
from app.db.enums import UserRole

from .conftest import make_settings


async def test_hash_verify_round_trip(auth_settings):
    hashed = await hash_password("probation123", settings=auth_settings)

    assert hashed != "probation123"
    assert await verify_password("probation123", hashed, settings=auth_settings) is True
    assert await verify_password("wrong", hashed, settings=auth_settings) is False


async def test_hash_is_salted(auth_settings):
    a = await hash_password("same", settings=auth_settings)
    b = await hash_password("same", settings=auth_settings)

    assert a != b


async def test_unknown_email_costs_a_real_verify(auth_settings):
    """AC-11: hashed=None must not short-circuit, or /token leaks account existence by timing
    regardless of what the response body says."""
    settings = make_settings(BCRYPT_ROUNDS=10)
    real = await hash_password("pw", settings=settings)

    t0 = time.perf_counter()
    assert await verify_password("wrong", real, settings=settings) is False
    known_email_s = time.perf_counter() - t0

    _dummy_hash(settings.BCRYPT_ROUNDS)  # exclude the one-off dummy build from the measurement
    t0 = time.perf_counter()
    assert await verify_password("wrong", None, settings=settings) is False
    unknown_email_s = time.perf_counter() - t0

    assert unknown_email_s > known_email_s * 0.5


def test_api_key_hash_is_stable_and_sha256():
    assert api_key_hash("crag_abc") == api_key_hash("crag_abc")
    assert len(api_key_hash("crag_abc")) == 64
    assert api_key_hash("crag_abc") != api_key_hash("crag_abd")


def test_new_api_key_is_prefixed_and_high_entropy():
    raw, hashed = new_api_key()

    assert raw.startswith("crag_")
    assert len(raw) > 40
    assert hashed == api_key_hash(raw)
    assert new_api_key()[0] != raw


def test_access_token_round_trip(auth_settings):
    uid = uuid.uuid4()
    token = encode_access(uid, UserRole.student, sid="fam-1", settings=auth_settings)

    claims = decode_token(token, expect="access", settings=auth_settings)

    assert claims["sub"] == str(uid)
    assert claims["role"] == "student"
    assert claims["sid"] == "fam-1"
    assert claims["typ"] == "access"
    assert claims["jti"]


def test_refresh_token_round_trip(auth_settings):
    uid = uuid.uuid4()
    token, jti, expires_at = encode_refresh(uid, UserRole.student, settings=auth_settings)

    claims = decode_token(token, expect="refresh", settings=auth_settings)

    assert claims["jti"] == jti
    assert claims["typ"] == "refresh"
    assert claims["exp"] == int(expires_at.timestamp())
    assert claims["exp"] > claims["iat"]


def test_tampered_token_rejected(auth_settings):
    token = encode_access(uuid.uuid4(), UserRole.student, sid="f", settings=auth_settings)
    head, payload, sig = token.split(".")

    with pytest.raises(AuthError) as exc:
        decode_token(f"{head}.{payload}x.{sig}", expect="access", settings=auth_settings)
    assert exc.value.status == 401


def test_token_from_a_different_secret_rejected(auth_settings):
    other = make_settings(JWT_SECRET="a-different-secret", BCRYPT_ROUNDS=4)
    token = encode_access(uuid.uuid4(), UserRole.student, sid="f", settings=other)

    with pytest.raises(AuthError):
        decode_token(token, expect="access", settings=auth_settings)


@pytest.mark.parametrize(
    "skew_s, should_pass",
    [(-29, True), (-31, False)],
    ids=["29s_expired_within_leeway", "31s_expired_beyond_leeway"],
)
def test_expiry_leeway(auth_settings, skew_s, should_pass):
    """AC-25: 30s of clock skew is tolerated, 31s is not."""
    claims = {
        "sub": str(uuid.uuid4()),
        "role": "student",
        "jti": "j",
        "sid": "f",
        "typ": "access",
        "exp": int(time.time()) + skew_s,
    }
    token = jwt.encode(claims, auth_settings.JWT_SECRET.get_secret_value(), algorithm="HS256")

    if should_pass:
        assert decode_token(token, expect="access", settings=auth_settings)["sid"] == "f"
    else:
        with pytest.raises(AuthError):
            decode_token(token, expect="access", settings=auth_settings)


def test_access_token_is_not_a_refresh_token(auth_settings):
    """AC-21: typ separates them, so an access token cannot be posted to /refresh."""
    access = encode_access(uuid.uuid4(), UserRole.student, sid="f", settings=auth_settings)

    with pytest.raises(AuthError) as exc:
        decode_token(access, expect="refresh", settings=auth_settings)
    assert exc.value.reason == "wrong_typ"


def test_every_failure_mode_returns_the_same_detail(auth_settings):
    refresh, _, _ = encode_refresh(uuid.uuid4(), UserRole.student, settings=auth_settings)
    expired = jwt.encode(
        {"typ": "access", "exp": int(time.time()) - 999},
        auth_settings.JWT_SECRET.get_secret_value(),
        algorithm="HS256",
    )
    details = set()
    for token, expect in [("not.a.token", "access"), (expired, "access"), (refresh, "access")]:
        with pytest.raises(AuthError) as exc:
            decode_token(token, expect=expect, settings=auth_settings)
        details.add(exc.value.detail)

    assert details == {GENERIC}
