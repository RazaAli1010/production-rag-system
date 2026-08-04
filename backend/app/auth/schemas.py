import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, model_validator

from app.db.enums import UserRole

# bcrypt hashes only the first 72 bytes; anything past that is silently ignored, so a longer
# password is a false sense of security rather than a stronger one.
PASSWORD_MAX = 72


def password_issues(password: str, email: str | None = None) -> list[str]:
    """The password policy, in one place. Empty list == acceptable.

    Mirrored in frontend/src/auth/passwordRules.ts for live form feedback; this copy is the one
    that enforces.
    """
    issues = []
    if len(password) < 8:
        issues.append("at least 8 characters")
    if not any(c.islower() for c in password):
        issues.append("a lowercase letter")
    if not any(c.isupper() for c in password):
        issues.append("an uppercase letter")
    if not any(c.isdigit() for c in password):
        issues.append("a number")
    local = (email or "").split("@")[0]
    if len(local) >= 3 and local.lower() in password.lower():
        issues.append("to not contain your email address")
    if len(password.encode()) > PASSWORD_MAX:
        issues.append(f"at most {PASSWORD_MAX} characters")
    return issues


class Principal(BaseModel):
    # `kind` is separate from `role` so an api_key belonging to the admin user still resolves as
    # kind="api_key" and stays ask-only.
    kind: Literal["student", "admin", "api_key"]
    user_id: uuid.UUID
    email: str
    role: UserRole
    api_key_id: uuid.UUID | None = None

    @property
    def is_admin(self) -> bool:
        return self.kind == "admin"


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    password: str

    @model_validator(mode="after")
    def _check_password(self) -> "RegisterRequest":
        # After-mode (not a field validator) because the email-similarity rule needs both fields.
        issues = password_issues(self.password, self.email)
        if issues:
            raise ValueError("Password needs " + ", ".join(issues) + ".")
        return self


class ForgotPasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr


class ResetPasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str
    password: str

    @model_validator(mode="after")
    def _check_password(self) -> "ResetPasswordRequest":
        # Same policy as signup — a reset must not be a way around it. No email to compare against
        # here; the server-side rule that needs it is enforced at registration only.
        issues = password_issues(self.password)
        if issues:
            raise ValueError("Password needs " + ", ".join(issues) + ".")
        return self


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    role: UserRole
    is_active: bool
    created_at: datetime
