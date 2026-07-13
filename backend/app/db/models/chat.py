"""`sessions`, `messages` — F17 state (design.md §3.4).

Note the `sessions.summarized_upto_message_id → messages.id` FK is a circular reference with
`sessions ← messages.session_id`; resolved with `use_alter=True` so Alembic emits the FK as a
post-create `ALTER TABLE` (see migrations/versions/0001_initial.py).
"""

import uuid

from sqlalchemy import Enum, ForeignKey, Index, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.enums import MessageRole
from app.db.types import CreatedAt, JSONBDict, TZDateTime, UUIDpk


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[UUIDpk]
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )  # anonymous allowed (AC-3.5)
    title: Mapped[str | None]  # auto from first question
    total_tokens: Mapped[int] = mapped_column(default=0)  # running tiktoken sum of ALL messages
    summary: Mapped[str | None]
    summary_token_count: Mapped[int | None]
    summarized_upto_message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL", use_alter=True)
    )
    created_at: Mapped[CreatedAt]
    last_active_at: Mapped[TZDateTime] = mapped_column(server_default=func.now())
    is_archived: Mapped[bool] = mapped_column(default=False)

    __table_args__ = (Index("ix_sessions_user_id_last_active_at", "user_id", "last_active_at"),)


class Message(Base):  # mirrors ChatMessage
    __tablename__ = "messages"

    id: Mapped[UUIDpk]
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"))
    role: Mapped[MessageRole] = mapped_column(Enum(MessageRole, name="message_role"))
    content: Mapped[str]
    token_count: Mapped[int]  # tiktoken cl100k_base
    citations: Mapped[JSONBDict | None]  # list[Citation] serialized; assistant turns only
    refused: Mapped[bool] = mapped_column(default=False)
    request_id: Mapped[str | None]
    created_at: Mapped[CreatedAt]

    __table_args__ = (Index("ix_messages_session_id_created_at", "session_id", "created_at"),)
