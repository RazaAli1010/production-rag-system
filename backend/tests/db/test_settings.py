"""T-1: Settings loads from env; missing DATABASE_URL raises; defaults match design.md §6."""

import pytest
from pydantic import ValidationError

from app.core.settings import Settings


def test_settings_loads_from_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost:5432/db")
    monkeypatch.setenv("ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("ADMIN_PASSWORD", "secret")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("PINECONE_API_KEY", "pc-test")
    monkeypatch.setenv("PINECONE_INDEX", "campus-rag")

    s = Settings(_env_file=None)

    assert str(s.DATABASE_URL).startswith("postgresql+asyncpg://")
    assert s.ADMIN_EMAIL == "admin@example.com"
    assert s.ADMIN_PASSWORD.get_secret_value() == "secret"


def test_settings_defaults(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost:5432/db")
    monkeypatch.setenv("ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("ADMIN_PASSWORD", "secret")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("PINECONE_API_KEY", "pc-test")
    monkeypatch.setenv("PINECONE_INDEX", "campus-rag")

    s = Settings(_env_file=None)

    assert s.DB_POOL_SIZE == 5
    assert s.DB_MAX_OVERFLOW == 2
    assert s.DB_POOL_TIMEOUT == 30
    assert s.DB_POOL_RECYCLE == 1800
    assert s.DB_STATEMENT_CACHE_SIZE == 0
    assert s.DB_ECHO is False


def test_missing_database_url_raises(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("ADMIN_PASSWORD", "secret")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)
