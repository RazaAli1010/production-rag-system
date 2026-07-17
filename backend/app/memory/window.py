"""The sliding-window + summarization rule (design §3.1, AC-18/19/20/21).

Pure CPU: no DB, no LLM. `assemble` only decides the SHAPE of this turn's prompt context — the
summariser *call* is the caller's job (`service.load_memory` decides whether one is due; `ask.py`
runs it). The three states, verbatim from the spec:

    | session state          | prompt context                                    |
    | ≤5 pairs               | all pairs verbatim + current question             |
    | >5 pairs, under 50k    | rolling summary + last 5 pairs + current question |
    | over 50k budget        | rolling summary + last 2 pairs + current question |

The "current question" is NOT part of the returned pairs — it is the just-persisted trailing user
message (AC-10) and is handled by the pipeline as `query`. `_pairs` drops that trailing unpaired
user message, so history is only *completed* (user, assistant) pairs.
"""

from app.core.contracts import ChatMessage, MemoryContext
from app.db.models.chat import Message, Session


def _pairs(messages: list[Message]) -> list[tuple[Message, Message]]:
    """Group an oldest→newest message list into whole (user, assistant) pairs.

    The pipeline guarantees strict alternation (user persisted, then assistant written-behind), so a
    pair is simply an adjacent user→assistant span. A trailing lone user message — the current
    question, persisted before this turn's pipeline runs — has no assistant reply yet and is dropped
    (it is asked separately as `query`). Refused assistant turns still form a pair and are kept
    verbatim in the window (AC-28); only the SUMMARY excludes their content.
    """
    pairs: list[tuple[Message, Message]] = []
    i = 0
    while i < len(messages) - 1:
        a, b = messages[i], messages[i + 1]
        if a.role.value == "user" and b.role.value == "assistant":
            pairs.append((a, b))
            i += 2
        else:
            i += 1  # skip an unpaired/out-of-order message defensively
    return pairs


def _last_whole_pairs(messages: list[Message], n: int) -> list[Message]:
    """The last `n` whole pairs, flattened back to an ordered message list (AC-21 — pairs move as a
    unit, never split even when one message is very large)."""
    pairs = _pairs(messages)[-n:] if n > 0 else []
    return [m for pair in pairs for m in pair]


def assemble(session: Session, recent: list[Message], settings) -> MemoryContext:
    """`recent` = the last-`window` messages already ordered oldest→newest (loaded by
    `service.load_memory` with `ORDER BY created_at DESC LIMIT ... ` then reversed).

    Over budget → shrink the verbatim window to `MEMORY_KEEP_LAST_PAIRS` and mark `summarized`
    (AC-20). `summary` is `session.summary`, which is `None` until the first pair slides out and is
    summarised — so the ≤5-pairs state carries no summary for free (AC-18).
    """
    over_budget = session.total_tokens >= settings.MEMORY_TOKEN_BUDGET
    window_pairs = settings.MEMORY_KEEP_LAST_PAIRS if over_budget else settings.MEMORY_WINDOW_PAIRS
    kept = _last_whole_pairs(recent, window_pairs)
    effective = (session.summary_token_count or 0) + sum(m.token_count for m in kept)
    return MemoryContext(
        summary=session.summary,
        pairs=[ChatMessage(role=m.role.value, content=m.content) for m in kept],
        window_pairs=window_pairs,
        effective_tokens=effective,
        summarized=over_budget,
    )
