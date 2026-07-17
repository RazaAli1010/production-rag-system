"""T3 — the sliding-window + budget rule (AC-18/19/20/21). Pure function, no DB/LLM."""

from app.db.enums import MessageRole
from app.db.models.chat import Message, Session

from .conftest import make_settings


def _msg(role: str, content: str, tokens: int = 5) -> Message:
    return Message(role=MessageRole(role), content=content, token_count=tokens)


def _pairs(n: int) -> list[Message]:
    """n completed (user, assistant) pairs, oldest→newest."""
    out: list[Message] = []
    for i in range(n):
        out.append(_msg("user", f"q{i}"))
        out.append(_msg("assistant", f"a{i}"))
    return out


def _session(total_tokens: int, summary: str | None = None, summary_tokens: int | None = None) -> Session:
    return Session(total_tokens=total_tokens, summary=summary, summary_token_count=summary_tokens)


def test_le5_pairs_all_verbatim_no_summary():
    from app.memory import window

    s = make_settings()
    recent = _pairs(3) + [_msg("user", "current-question")]  # trailing current q must be dropped
    ctx = window.assemble(_session(total_tokens=100), recent, s)

    assert ctx.summary is None
    assert ctx.summarized is False
    assert [m.content for m in ctx.pairs] == ["q0", "a0", "q1", "a1", "q2", "a2"]
    assert "current-question" not in [m.content for m in ctx.pairs]


def test_gt5_under_budget_last5_plus_summary():
    from app.memory import window

    s = make_settings()
    recent = _pairs(8) + [_msg("user", "current-question")]
    ctx = window.assemble(_session(total_tokens=1000, summary="rolling summary", summary_tokens=50), recent, s)

    assert ctx.summary == "rolling summary"
    assert ctx.summarized is False
    assert ctx.window_pairs == 5
    contents = [m.content for m in ctx.pairs]
    assert contents == ["q3", "a3", "q4", "a4", "q5", "a5", "q6", "a6", "q7", "a7"]  # last 5 pairs
    assert "q0" not in contents and "q2" not in contents  # pairs 1-3 only live in the summary


def test_over_budget_shrinks_to_last2_pairs():
    from app.memory import window

    s = make_settings()
    recent = _pairs(8) + [_msg("user", "current-question")]
    ctx = window.assemble(_session(total_tokens=50_001, summary="rolling summary", summary_tokens=40), recent, s)

    assert ctx.summarized is True
    assert ctx.window_pairs == 2
    contents = [m.content for m in ctx.pairs]
    assert contents == ["q6", "a6", "q7", "a7"]  # last 2 pairs, nothing older
    # effective = summary(40) + 4 messages * 5 tokens = 60, well under the 50k budget
    assert ctx.effective_tokens < s.MEMORY_TOKEN_BUDGET


def test_huge_message_keeps_pair_whole():
    from app.memory import window

    s = make_settings()
    recent = _pairs(4) + [_msg("user", "q4"), _msg("assistant", "a4-huge", tokens=4000),
                          _msg("user", "current-question")]
    ctx = window.assemble(_session(total_tokens=100), recent, s)

    # the 4000-token answer's pair is kept whole, not split
    assert ["q4", "a4-huge"] == [m.content for m in ctx.pairs[-2:]]
    assert ctx.effective_tokens >= 4000
