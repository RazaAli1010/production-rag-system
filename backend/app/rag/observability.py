"""Langfuse callback (optional, None-safe) + cost logging (design.md §8, AC-25/26).

`langfuse_handler` takes `settings` explicitly (design.md §4's one-arg signature is adjusted
here, same as `retriever.retrieve`/`refusal.pre_llm_gate`) because whether it returns a handler
is entirely config-dependent (`LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` present or not) — the
module-level `Settings()` singleton is constructed once at import time, so it can't reflect
per-test env overrides the way an explicit, freshly-constructed `Settings` instance can.
"""

import structlog

from app.indexing.cost import estimate_cost

logger = structlog.get_logger(__name__)


def langfuse_handler(session_id: str | None, settings):
    """Returns a Langfuse `CallbackHandler` when both Langfuse keys are configured, else `None`
    — Langfuse is optional, never a hard boot requirement (Settings `LANGFUSE_*` default to
    `None`). Callers attach it via `config={"callbacks": [h] if h else []}` (AC-25)."""
    if settings.LANGFUSE_PUBLIC_KEY is None or settings.LANGFUSE_SECRET_KEY is None:
        return None
    try:
        from langfuse.callback import CallbackHandler
    except ImportError:
        logger.warning("rag.langfuse_not_installed")
        return None
    return CallbackHandler(
        public_key=settings.LANGFUSE_PUBLIC_KEY.get_secret_value(),
        secret_key=settings.LANGFUSE_SECRET_KEY.get_secret_value(),
        host=settings.LANGFUSE_HOST,
        session_id=session_id,
    )


async def log_llm_cost(model: str, tokens_in: int, tokens_out: int = 0) -> None:
    """`estimate_cost()` is F2's central cost helper, reused verbatim (AC-26)."""
    cost = estimate_cost(model, tokens_in, tokens_out)
    logger.info("rag.llm_cost", model=model, tokens_in=tokens_in, tokens_out=tokens_out,
               est_cost_usd=cost)
