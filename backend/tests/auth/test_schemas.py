import uuid
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.auth.schemas import Principal, RegisterRequest, UserOut, password_issues
from app.db.enums import UserRole
from app.db.models import User


STRONG = "Testpass123"


def test_register_request_rejects_a_role_field():
    """AC-6: role must not be settable by the request body."""
    with pytest.raises(ValidationError):
        RegisterRequest(email="s@pu.edu.pk", password=STRONG, role="admin")


@pytest.mark.parametrize(
    "password",
    [
        "short",
        "1234567",
        "Ab1",
        "testpass123",  # no uppercase
        "TESTPASS123",  # no lowercase
        "Testpassword",  # no digit
        "A1" + "a" * 71,  # past bcrypt's 72-byte window
    ],
    ids=["short", "seven", "short_but_mixed", "no_upper", "no_lower", "no_digit", "too_long"],
)
def test_register_request_rejects_weak_passwords(password):
    with pytest.raises(ValidationError):
        RegisterRequest(email="student@pu.edu.pk", password=password)


def test_register_request_accepts_a_strong_password():
    assert RegisterRequest(email="s@pu.edu.pk", password=STRONG).password == STRONG


def test_password_may_not_be_built_from_the_email():
    """"bilal" + digits is the most common student password shape; it is also the one an
    attacker guesses first from the address."""
    assert password_issues("Bilal1234", "bilal@pu.edu.pk") == ["to not contain your email address"]
    # A short local-part is not a signal — refusing every password containing "s" is nonsense.
    assert password_issues("Sabcd1234", "s@pu.edu.pk") == []


def test_password_issues_names_every_unmet_rule():
    assert password_issues("abc") == ["at least 8 characters", "an uppercase letter", "a number"]


def test_register_request_rejects_a_bad_email():
    with pytest.raises(ValidationError):
        RegisterRequest(email="not-an-email", password=STRONG)


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
