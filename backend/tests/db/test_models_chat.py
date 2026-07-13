"""T-7: Session/Message — anonymous sessions, circular FK, JSONB citations round-trip."""

import pytest

from app.db.enums import MessageRole
from app.db.models import Message
from app.db.models import Session as ChatSession


@pytest.mark.asyncio
async def test_anonymous_session_and_messages(session):
    chat_session = ChatSession(user_id=None, total_tokens=0, is_archived=False)
    session.add(chat_session)
    await session.flush()
    assert chat_session.id is not None

    citations = [
        {"doc_id": "hec-plagiarism-policy-2021", "section": "3.2", "page": 4, "quote": "..."}
    ]
    user_msg = Message(
        session_id=chat_session.id,
        role=MessageRole.user,
        content="probation se kaise nikalta hoon",
        token_count=6,
        citations=None,
        refused=False,
    )
    assistant_msg = Message(
        session_id=chat_session.id,
        role=MessageRole.assistant,
        content="Per section 3.2 [1]...",
        token_count=12,
        citations=citations,
        refused=False,
    )
    session.add_all([user_msg, assistant_msg])
    await session.flush()

    fetched_assistant = await session.get(Message, assistant_msg.id)
    assert fetched_assistant.citations == citations  # JSONB round-trip

    chat_session.summarized_upto_message_id = user_msg.id
    await session.flush()
    refetched = await session.get(ChatSession, chat_session.id)
    assert refetched.summarized_upto_message_id == user_msg.id
