"""Rolling-summary extension — one `gpt-4o-mini` call, amortised to ~1 per 3 slid-out pairs
(design §3.2, AC-23/24/28).

`extend_summary` consumes ONLY the old summary + the pending (slid-out, not-yet-summarised) pairs —
never the whole transcript (AC-24). It RAISES on any failure; the caller (`ask.py`) wraps it in the
`memory.summarize_failed` fallback (AC-27) so a summary failure never blocks answering. LLM cost is
logged through the central `log_llm_cost`, like every other OpenAI call in the app.
"""

from langchain_openai import ChatOpenAI

from app.db.models.chat import Message
from app.rag import observability

_SYSTEM = """\
You maintain a running summary of a chat between a student and an assistant about University of the
Punjab (PU) and HEC regulations. Extend the existing summary with the new turns below. Record: facts
the student asked about, answers given, documents cited, and any unresolved threads. Keep it concise
and factual — do not invent anything not present in the turns. A turn marked [REFUSED] means the
assistant could not answer; note the open question but do NOT record a fabricated answer for it."""


def _build_llm(settings) -> ChatOpenAI:
    return ChatOpenAI(
        model=settings.MEMORY_SUMMARY_MODEL,
        temperature=settings.MEMORY_SUMMARY_TEMPERATURE,
        max_tokens=settings.MEMORY_SUMMARY_MAX_TOKENS,
        api_key=settings.OPENAI_API_KEY.get_secret_value(),
    )


def _render_pending(pending: list[Message]) -> str:
    lines = []
    for m in pending:
        tag = " [REFUSED]" if getattr(m, "refused", False) else ""
        lines.append(f"{m.role.value}{tag}: {m.content}")
    return "\n".join(lines)


async def extend_summary(old_summary: str | None, pending: list[Message], settings) -> str:
    """`old_summary + pending → new_summary`. Raises on provider/parse failure (caller handles)."""
    llm = _build_llm(settings)
    prior = old_summary or "(no summary yet)"
    human = f"Existing summary:\n{prior}\n\nNew turns to fold in:\n{_render_pending(pending)}"
    msg = await llm.ainvoke([("system", _SYSTEM), ("human", human)])
    new_summary = msg.content.strip()

    tokens_in = len((_SYSTEM + human).split())  # rough; exact accounting via usage when available
    usage = getattr(msg, "usage_metadata", None) or {}
    await observability.log_llm_cost(
        settings.MEMORY_SUMMARY_MODEL,
        usage.get("input_tokens", tokens_in),
        usage.get("output_tokens", len(new_summary.split())),
    )
    return new_summary
