"""Langfuse callback (optional, no-op-safe) + cost logging (design.md §8, AC-25/26).

`langfuse_config` takes `settings` explicitly (design.md §4's one-arg signature is adjusted
here, same as `retriever.retrieve`/`refusal.pre_llm_gate`) because whether it returns a callback
is entirely config-dependent (`LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` present or not) — the
module-level `Settings()` singleton is constructed once at import time, so it can't reflect
per-test env overrides the way an explicit, freshly-constructed `Settings` instance can.

langfuse>=4 splits what the v2 handler took in one constructor: CREDENTIALS live on a process-wide
client (`init_langfuse`, once, from the app lifespan) and PER-TRACE attributes ride in the
invocation `config["metadata"]` under `langfuse_*` keys. Hence two functions where there was one.
"""

import structlog

from app.core.middleware import request_id_var
from app.indexing.cost import estimate_cost
from app.observability.metrics import record_cost

logger = structlog.get_logger(__name__)


def init_langfuse(settings) -> None:
    """Construct the process-wide Langfuse client, once, at startup. No-op (with a log line saying
    which) when the keys are absent or the package isn't installed — Langfuse is optional and never
    a hard boot requirement.

    This has to be explicit rather than left to the SDK's own env-var pickup: pydantic-settings
    reads `.env` into `Settings` WITHOUT exporting to `os.environ`, so a `.env`-only configuration
    is invisible to `Langfuse()`'s auto-config and every trace would be silently dropped."""
    if settings.LANGFUSE_PUBLIC_KEY is None or settings.LANGFUSE_SECRET_KEY is None:
        logger.info("rag.langfuse_disabled", reason="keys_not_configured")
        return
    try:
        from langfuse import Langfuse
    except ImportError:
        logger.warning("rag.langfuse_not_installed")
        return
    Langfuse(
        public_key=settings.LANGFUSE_PUBLIC_KEY.get_secret_value(),
        secret_key=settings.LANGFUSE_SECRET_KEY.get_secret_value(),
        base_url=settings.LANGFUSE_BASE_URL,
        environment=settings.APP_ENV,
        release=settings.APP_VERSION,  # F15: which release produced this trace
    )
    logger.info("rag.langfuse_enabled", base_url=settings.LANGFUSE_BASE_URL,
                environment=settings.APP_ENV, release=settings.APP_VERSION)


def flush_langfuse() -> None:
    """Blocking — call via `anyio.to_thread.run_sync` from the async lifespan teardown. The SDK
    batches spans on a background thread, so without this the tail of in-flight traces dies with
    the process on shutdown."""
    try:
        from langfuse import get_client
    except ImportError:
        return
    get_client().flush()


def langfuse_config(session_id: str | None, settings) -> dict:
    """The LCEL `config` to pass at an LLM call site: `{"callbacks": [...], "metadata": {...}}` when
    Langfuse is configured, else `{}` (AC-25). Callers pass the result straight through as
    `config=`, so an unconfigured deployment threads an empty dict and behaves exactly as before.

    `CallbackHandler()` takes no credentials in v4 — it resolves the client `init_langfuse` built.
    Trace attributes travel as `langfuse_*` metadata keys instead of constructor kwargs; plain
    `request_id` stays a normal metadata field so the runbook's `metadata.request_id` filter keeps
    resolving a request to its trace (AC-1)."""
    if settings.LANGFUSE_PUBLIC_KEY is None or settings.LANGFUSE_SECRET_KEY is None:
        return {}
    try:
        from langfuse.langchain import CallbackHandler
    except ImportError:
        logger.warning("rag.langfuse_not_installed")
        return {}
    # ponytail: one trace per LLM call, not one trace per request with the rewrite/summarizer as
    # child spans. v4 is OTel-based, so nesting means holding a `start_as_current_span` open across
    # the pipeline's async-generator yields — context propagation there is exactly the fiddly part.
    # The shared `request_id` (the correlation key `docs/runbook.md` already documents) groups them
    # meanwhile; upgrade to a wrapping span if the flat view stops being enough.
    metadata = {"langfuse_trace_name": "ask", "request_id": request_id_var.get()}
    if session_id is not None:
        metadata["langfuse_session_id"] = session_id
    return {"callbacks": [CallbackHandler()], "metadata": metadata}


async def log_llm_cost(model: str, tokens_in: int, tokens_out: int = 0) -> None:
    """`estimate_cost()` is F2's central cost helper, reused verbatim (AC-26).

    F13: also accumulates the spend into the per-request metrics (no-op off an ask path), so
    `request_logs.est_cost_usd` sums every OpenAI call the request made."""
    cost = estimate_cost(model, tokens_in, tokens_out)
    record_cost(model, tokens_in, tokens_out)
    logger.info("rag.llm_cost", model=model, tokens_in=tokens_in, tokens_out=tokens_out,
               est_cost_usd=cost)


def log_rewrite(
    rewrite_ms: int, n_variants: int, n_fanout: int, language: str | None, failed: bool
) -> None:
    """F7: record the query-rewrite metrics (AC-19). The rewrite's OpenAI cost is logged separately
    via `log_llm_cost(settings.REWRITE_MODEL, …)` (gpt-4o-mini) in `rewrite.rewrite_query`; this
    record carries the latency + shape of the rewrite (variants, fan-out size, chosen answer
    language, and whether the raw-query fallback was taken). Synchronous + non-blocking (a structlog
    emit over a handful of values), mirroring `log_rerank`; F13 later routes it into
    `request_logs`/Langfuse without an F7 change."""
    logger.info("rag.rewrite", rewrite_ms=rewrite_ms, n_variants=n_variants, n_fanout=n_fanout,
                language=language, rewrite_failed=failed)


def log_compression(
    tokens_before: int,
    tokens_after: int,
    chunks_before: int,
    chunks_after: int,
    sentences_dropped: int,
    compression_ms: int,
) -> None:
    """F8: record the context-compression metrics (AC-12). Compression adds no OpenAI call — the F6
    cross-encoder is reused for sentence scoring — so there is no `estimate_cost` site here; the
    cost
    win surfaces as fewer generation input tokens through the existing `log_llm_cost`. Synchronous +
    non-blocking (a structlog emit over a handful of numbers), mirroring `log_rerank`/`log_rewrite`;
    F13 later routes it into `request_logs`/Langfuse without an F8 change."""
    logger.info("rag.compression", tokens_before=tokens_before, tokens_after=tokens_after,
                chunks_before=chunks_before, chunks_after=chunks_after,
                chunks_dropped=chunks_before - chunks_after, sentences_dropped=sentences_dropped,
                compression_ms=compression_ms)


def log_cache(
    hit: bool,
    tier: str,
    lookup_ms: int,
    n_entries: int,
    cosine: float | None = None,
    tokens_saved: int = 0,
    est_cost_saved_usd: float = 0.0,
) -> None:
    """F9: record the semantic-cache metrics (AC-26). `tier` is `redis` | `semantic` | `miss`.

    The cache adds no OpenAI call — it REMOVES them — so there is no `estimate_cost` site here:
    `est_cost_saved_usd` is computed by the caller via F2's central `estimate_cost` over the cached
    response's `tokens_in`/`tokens_out` (AC-27), i.e. the spend this hit avoided. Synchronous +
    non-blocking (a structlog emit over a handful of values), mirroring `log_rerank`/`log_rewrite`/
    `log_compression`; F13 later routes it into `request_logs`/Langfuse (the `cache_hit` and
    `embed_ms` columns already exist) without an F9 change."""
    logger.info("rag.cache", cache_hit=hit, tier=tier, lookup_ms=lookup_ms,
                n_entries=n_entries, cosine=cosine, tokens_saved=tokens_saved,
                est_cost_saved_usd=est_cost_saved_usd)


def log_rerank(rerank_ms: int, max_score: float, n_candidates: int) -> None:
    """F6: record the cross-encoder rerank metrics (AC-20). Reranking adds no OpenAI call — the
    cross-encoder is free/in-process — so there is no `estimate_cost` site here; the only new
    metric is CPU time (`rerank_ms`, bounded < 300ms p50 by AC-8) plus the calibrated confidence.
    Synchronous + non-blocking (a structlog emit over a handful of numbers), mirroring the F3/F5
    convention; F13 later routes this record into `request_logs`/Langfuse without an F6 change."""
    logger.info("rag.rerank", rerank_ms=rerank_ms, max_rerank_score=max_score,
                n_candidates=n_candidates)
