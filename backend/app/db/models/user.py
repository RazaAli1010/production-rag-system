"""`users`, `api_keys` — owned schema, F10 logic (design.md §3.1)."""

import uuid

from sqlalchemy import Enum, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.enums import UserRole
from app.db.types import CreatedAt, TZDateTime, UUIDpk


class User(Base):
    __tablename__ = "users"

    id: Mapped[UUIDpk]
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    hashed_password: Mapped[str]
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, name="user_role"), default=UserRole.student
    )
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[CreatedAt]


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[UUIDpk]
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    key_hash: Mapped[str]
    label: Mapped[str | None]
    created_at: Mapped[CreatedAt]
    revoked_at: Mapped[TZDateTime | None]
