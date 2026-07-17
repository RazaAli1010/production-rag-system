"""T6 — anon session cookie round-trips; forged/tampered/wrong-typ verify to None (AC-8)."""

import uuid

import jwt

from app.memory import cookies

from .conftest import make_settings


def test_roundtrip():
    s = make_settings()
    sid = uuid.uuid4()
    assert cookies.verify(cookies.sign(sid, settings=s), settings=s) == sid


def test_none_and_empty():
    s = make_settings()
    assert cookies.verify(None, settings=s) is None
    assert cookies.verify("", settings=s) is None


def test_tampered_token_is_none():
    s = make_settings()
    token = cookies.sign(uuid.uuid4(), settings=s)
    assert cookies.verify(token + "x", settings=s) is None


def test_wrong_secret_is_none():
    s = make_settings(JWT_SECRET="secret-a")
    token = cookies.sign(uuid.uuid4(), settings=s)
    assert cookies.verify(token, settings=make_settings(JWT_SECRET="secret-b")) is None


def test_wrong_typ_is_none():
    s = make_settings()
    # a token with a different typ (e.g. an access-like token) must not be accepted as anon session
    forged = jwt.encode({"sid": str(uuid.uuid4()), "typ": "access"},
                        s.JWT_SECRET.get_secret_value(), algorithm=s.JWT_ALGORITHM)
    assert cookies.verify(forged, settings=s) is None
