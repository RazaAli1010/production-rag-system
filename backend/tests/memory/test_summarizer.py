"""T4 — summariser folds old+pending only, never the transcript (AC-24); errors propagate (AC-27)."""

import pytest

from app.db.enums import MessageRole
from app.db.models.chat import Message
from app.memory import summarizer

from .conftest import make_settings


class _FakeMsg:
    def __init__(self, content):
        self.content = content
        self.usage_metadata = {"input_tokens": 10, "output_tokens": 5}


class _FakeLLM:
    def __init__(self):
        self.seen = None

    async def ainvoke(self, messages):
        self.seen = messages
        return _FakeMsg("extended summary")


def _pending():
    return [
        Message(role=MessageRole.user, content="q-OLD-1", token_count=3),
        Message(role=MessageRole.assistant, content="a-OLD-1", token_count=3),
    ]


async def test_prompt_contains_only_old_and_pending(monkeypatch):
    fake = _FakeLLM()
    monkeypatch.setattr(summarizer, "_build_llm", lambda s: fake)
    # avoid a real cost-log call touching the rate table with a fake model
    monkeypatch.setattr(summarizer.observability, "log_llm_cost", _noop)

    out = await summarizer.extend_summary("PRIOR-SUMMARY", _pending(), make_settings())

    assert out == "extended summary"
    human = fake.seen[1][1]
    assert "PRIOR-SUMMARY" in human and "q-OLD-1" in human and "a-OLD-1" in human
    # the transcript's newer/window turns are NOT handed to the summariser
    assert "current" not in human.lower()


async def test_refused_turn_is_tagged(monkeypatch):
    fake = _FakeLLM()
    monkeypatch.setattr(summarizer, "_build_llm", lambda s: fake)
    monkeypatch.setattr(summarizer.observability, "log_llm_cost", _noop)
    pending = [
        Message(role=MessageRole.user, content="q", token_count=3),
        Message(role=MessageRole.assistant, content="a", token_count=3, refused=True),
    ]
    await summarizer.extend_summary(None, pending, make_settings())
    assert "[REFUSED]" in fake.seen[1][1]


async def test_llm_failure_propagates(monkeypatch):
    class _Boom:
        async def ainvoke(self, messages):
            raise RuntimeError("provider 500")

    monkeypatch.setattr(summarizer, "_build_llm", lambda s: _Boom())
    with pytest.raises(RuntimeError):
        await summarizer.extend_summary("x", _pending(), make_settings())


async def _noop(*a, **k):
    return None
