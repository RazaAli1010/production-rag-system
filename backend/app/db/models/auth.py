"""`refresh_tokens`, `login_attempts` — the JWT blacklist + lockout feed (design.md §3.2).

Validity rule (used by F10, not implemented here):
    `revoked_at IS NULL AND expires_at > now()`
Lockout query (F10):
    `count(*) WHERE email_or_ip=:k AND success=false AND attempted_at > now()-15min`
The `(email_or_ip, attempted_at)` index backs that window; old rows pruned on schedule (F13/cron).
"""

import uuid

from sqlalchemy import ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import CreatedAt, TZDateTime, UUIDpk


class RefreshToken(Base):  # AC-3.6: this table IS the blacklist
    __tablename__ = "refresh_tokens"

    id: Mapped[UUIDpk]
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    jti: Mapped[str] = mapped_column(unique=True, index=True)
    issued_at: Mapped[CreatedAt]
    expires_at: Mapped[TZDateTime]
    revoked_at: Mapped[TZDateTime | None]  # NULL + not expired == valid
    replaced_by_jti: Mapped[str | None]  # rotation chain
    user_agent: Mapped[str | None]
    ip: Mapped[str | None]


class LoginAttempt(Base):  # AC-3.7: windowed lockout counter
    __tablename__ = "login_attempts"

    id: Mapped[UUIDpk]
    email_or_ip: Mapped[str] = mapped_column(index=True)
    attempted_at: Mapped[CreatedAt] = mapped_column(index=True)
    success: Mapped[bool]

    __table_args__ = (
        Index("ix_login_attempts_email_or_ip_attempted_at", "email_or_ip", "attempted_at"),
    )
