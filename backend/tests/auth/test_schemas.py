import uuid
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.auth.schemas import Principal, RegisterRequest, UserOut
from app.db.enums import UserRole
from app.db.models import User


def test_register_request_rejects_a_role_field():
    """AC-6: role must not be settable by the request body."""
    with pytest.raises(ValidationError):
        RegisterRequest(email="s@pu.edu.pk", password="testpass123", role="admin")


@pytest.mark.parametrize("password", ["short", "1234567"])
def test_register_request_enforces_min_password_length(password):
    with pytest.raises(ValidationError):
        RegisterRequest(email="s@pu.edu.pk", password=password)


def test_register_request_rejects_a_bad_email():
    with pytest.raises(ValidationError):
        RegisterRequest(email="not-an-email", password="testpass123")


def test_user_out_cannot_carry_the_hash():
    """AC-1: the response model has no field for it, so a hash cannot leak by accident."""
    user = User(
        id=uuid.uuid4(),
        email="s@pu.edu.pk",
        hashed_password="$2b$12$averysecrethash",
        role=UserRole.student,
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )

    out = UserOut.model_validate(user)

    assert "hashed_password" not in out.model_dump()
    assert "averysecrethash" not in out.model_dump_json()


def test_api_key_principal_is_never_admin_kind():
    p = Principal(
        kind="api_key", user_id=uuid.uuid4(), email="admin@pu.edu.pk", role=UserRole.admin
    )

    assert p.is_admin is False
