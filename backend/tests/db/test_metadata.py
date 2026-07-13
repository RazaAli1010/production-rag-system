"""T-2, T-10: Base/naming-convention/enum shape and full 12-table registration."""

import app.db.models  # noqa: F401 — registers all models on Base.metadata
from app.db.base import Base
from app.db.enums import DocumentStatus, MessageRole, RequestChannel, UserRole

EXPECTED_TABLES = {
    "users",
    "api_keys",
    "refresh_tokens",
    "login_attempts",
    "documents",
    "chunks",
    "sessions",
    "messages",
    "request_logs",
    "cache_entries",
    "eval_runs",
    "eval_results",
}


def test_naming_convention_has_five_keys():
    assert set(Base.metadata.naming_convention.keys()) >= {"ix", "uq", "fk", "pk", "ck"}


def test_enum_members():
    assert {m.value for m in UserRole} == {"student", "admin"}
    assert {m.value for m in DocumentStatus} == {
        "registered",
        "downloaded",
        "extracted",
        "indexed",
        "failed",
    }
    assert {m.value for m in MessageRole} == {"user", "assistant", "system"}
    assert {m.value for m in RequestChannel} == {"web", "telegram", "api"}


def test_exactly_twelve_tables_matching_shared_context():
    assert len(Base.metadata.tables) == 12
    assert set(Base.metadata.tables.keys()) == EXPECTED_TABLES
