import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.db.enums import UserRole


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
    password: str = Field(min_length=8)


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
