import pytest
from pydantic import ValidationError

from app.core.settings import Settings

from .conftest import make_settings


def test_auth_defaults():
    s = make_settings()

    assert s.JWT_ALGORITHM == "HS256"
    assert s.JWT_LEEWAY_S == 30
    assert s.ACCESS_TOKEN_TTL_MIN == 15
    assert s.REFRESH_TOKEN_TTL_DAYS == 7
    assert s.BCRYPT_ROUNDS == 12
    assert s.AUTH_EMAIL_DOMAIN_ALLOWLIST == []
    assert s.LOGIN_MAX_FAILURES == 10
    assert s.LOGIN_LOCKOUT_WINDOW_MIN == 15


def test_rate_tier_defaults_match_the_matrix():
    s = make_settings()

    assert s.RATE_LIMIT_ANON_PER_MIN == 5
    assert s.RATE_LIMIT_STUDENT_PER_MIN == 20
    assert s.RATE_LIMIT_ADMIN_PER_MIN == 60
    assert s.RATE_LIMIT_API_KEY_PER_MIN == 30


def test_jwt_secret_is_required(monkeypatch):
    monkeypatch.delenv("JWT_SECRET", raising=False)

    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            DATABASE_URL="postgresql+asyncpg://u:p@localhost:5432/db",
            ADMIN_EMAIL="a@b.c",
            ADMIN_PASSWORD="x",
            OPENAI_API_KEY="sk-test",
            PINECONE_API_KEY="pc-test",
            PINECONE_INDEX="campus-rag-test",
        )


def test_jwt_secret_is_not_exposed_by_repr():
    s = make_settings(JWT_SECRET="super-secret-value")

    assert "super-secret-value" not in repr(s)
    assert s.JWT_SECRET.get_secret_value() == "super-secret-value"
