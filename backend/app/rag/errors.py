"""Typed provider error + retry predicate (design.md §7, AC-21).

Reuses F2's `_is_rate_limit` shape verbatim (`app.indexing.embedder`) rather than reimplementing
it — same 429/name-based predicate, so the two features agree on what "retryable" means.
"""

import tenacity

from app.indexing.embedder import _is_rate_limit


class ProviderError(Exception):
    """Raised when the LLM/embeddings provider fails after the retry budget is exhausted.

    F11 maps this to HTTP 503 — out of scope here (requirements.md AC-21)."""


def is_retryable(exc: Exception) -> bool:
    return _is_rate_limit(exc)


async def call_with_retry(fn, *args, settings, **kwargs):
    """Tenacity wrapper (F2's exact shape: `retry_if_exception(is_retryable)`, exponential
    backoff, `reraise=True`) for a single-shot async call — the retrieval seam's embedding+query
    round trip. `LLM_MAX_RETRIES` retries beyond the first attempt, then raises `ProviderError`.
    A non-retryable exception (e.g. 400) propagates immediately, unwrapped, on the first try."""
    try:
        async for attempt in tenacity.AsyncRetrying(
            retry=tenacity.retry_if_exception(is_retryable),
            wait=tenacity.wait_exponential(min=1, max=30),
            stop=tenacity.stop_after_attempt(settings.LLM_MAX_RETRIES + 1),
            reraise=True,
        ):
            with attempt:
                return await fn(*args, **kwargs)
    except Exception as exc:
        if is_retryable(exc):
            raise ProviderError(str(exc)) from exc
        raise
