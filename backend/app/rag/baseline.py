"""The two public entry points (`answer`, `astream`) and the LCEL generation sub-chain
(design.md §4/§5). Driven only via the async `ainvoke`/`astream_events` surfaces (AC-5) — the
sync invoke/stream surfaces are never called anywhere in this module or the rest of `app/rag/`.
"""

import time
from collections.abc import AsyncIterator

import structlog
import tiktoken
from langchain_core.output_parsers import StrOutputParser
from langchain_core.runnables import RunnableLambda, RunnablePassthrough
from langchain_openai import ChatOpenAI
from pydantic import ValidationError

from app.caching import keys, redis_hot
from app.caching import store as cache_store
from app.core.contracts import AnswerResponse, MemoryContext
from app.db.engine import get_sessionmaker
from app.indexing.cost import estimate_cost
from app.memory import stages
from app.rag import citations as citations_mod
from app.rag import compression as compression_mod
from app.rag import context, observability, prompt, refusal, trace
from app.rag import errors as errors_mod
from app.rag import flags as flags_mod
from app.rag import hybrid as hybrid_mod
from app.rag import rerank as rerank_mod
from app.rag import retriever as retriever_mod
from app.rag import rewrite as rewrite_mod
from app.rag.events import SSEEvent
from app.rag.schemas import PipelineFlags

logger = structlog.get_logger(__name__)

# Mirrors app.indexing.chunkers.base's tiktoken cl100k_base truncate pattern, bound to
# MAX_QUERY_TOKENS instead of EMBED_MAX_CHUNK_TOKENS (AC-13) — a different Settings key, so it
# isn't the literal same function, but the same encode/decode/warn shape.
_ENC = tiktoken.get_encoding("cl100k_base")


def build_llm(settings):
    return ChatOpenAI(
        model=settings.LLM_MODEL,
        temperature=0,
        api_key=settings.OPENAI_API_KEY.get_secret_value(),
    )


def build_generate_chain(llm):
    """`format_context | prompt | llm | parser` (design.md §5).

    The literal design snippet pipes `RunnableLambda(format_context)` straight into
    `build_prompt()`, but the prompt template needs three variables (`context`, `memory_block`,
    `question`), not just `context` — so `RunnablePassthrough.assign` is the LCEL-idiomatic way
    to compute the `context` key from `format_context(chunks)` while passing `memory_block`/
    `question` through unchanged. Input shape: `{"chunks": list[RetrievedChunk], "memory_block":
    str, "question": str}`.
    """
    return (
        RunnablePassthrough.assign(
            context=RunnableLambda(lambda x: context.format_context(x["chunks"]))
        )
        | prompt.build_prompt()
        | llm
        | StrOutputParser()
    )


def _truncate_query(query: str, settings) -> str:
    tokens = _ENC.encode(query)
    if len(tokens) <= settings.MAX_QUERY_TOKENS:
        return query
    truncated = _ENC.decode(tokens[: settings.MAX_QUERY_TOKENS])
    logger.warning("rag.query_truncated", original_tokens=len(tokens),
                   max_tokens=settings.MAX_QUERY_TOKENS)
    return truncated


async def _stream_chain_with_retry(chain, chain_input, config, settings):
    """Retries the whole `astream_events` run from scratch when a failure happens *before* any
    token has been yielded (safe — nothing has reached the client yet). Once at least one token
    has been yielded, a failure is no longer retried here: it propagates so `_pipeline_events`
    can convert it into a terminal SSE `error` event (AC-22) instead of silently re-running a
    stream that's already been partially sent. This is the "Embeddings/LLM 429 or 5xx" retry
    path (AC-21) for the generation step specifically; `errors.call_with_retry` covers the
    single-shot retrieval call."""
    attempts = 0
    max_attempts = settings.LLM_MAX_RETRIES + 1
    while True:
        attempts += 1
        yielded_any = False
        try:
            async for event in chain.astream_events(chain_input, version="v2", config=config):
                if event["event"] == "on_chat_model_stream" and event["data"]["chunk"].content:
                    yielded_any = True
                yield event
            return
        except Exception as exc:
            if yielded_any or not errors_mod.is_retryable(exc) or attempts >= max_attempts:
                if errors_mod.is_retryable(exc) and not yielded_any:
                    raise errors_mod.ProviderError(str(exc)) from exc
                raise
            continue  # not yet yielded anything, retryable, budget remains: retry from scratch


async def _cache_lookup(query, memory, settings, sessionmaker):
    """F9's lookup seam (design §3). Returns `(hit_or_None, rr, normalized, query_vec, lookup_ms)`.

    Order matters and is the design: Redis exact-match is checked BEFORE embedding, so an exact
    repeat costs one Redis round-trip and no OpenAI call at all. Only on a hot miss do we embed —
    once — and that same vector is handed back for the miss path to retrieve with, so a miss is
    cheaper than the pre-F9 path rather than more expensive (design §2).

    `rr` is computed here because the cache key IS F7's standalone question; it is handed back so
    `rewrite.retrieve` does not pay for a second rewrite on a miss (AC-12).
    """
    t0 = time.monotonic()
    rr = None
    if settings.ENABLE_QUERY_REWRITE:
        rr = await rewrite_mod.rewrite_query(query, memory, settings)
    normalized = keys.normalize(rr.normalized if rr else query)

    # Tier 1 — exact match. No embed on this path (AC-1/AC-2).
    payload = await redis_hot.get(
        keys.exact_key(normalized, prefix=settings.CACHE_KEY_PREFIX), settings=settings
    )
    if payload is not None:
        try:
            hit = AnswerResponse.model_validate(payload)
        except ValidationError:
            hit = None  # redis_hot.get already dropped the corrupt key
        if hit is not None:
            ms = int((time.monotonic() - t0) * 1000)
            return hit, rr, normalized, None, ms

    # Tier 2 — semantic. THE one embed per request (AC-5).
    query_vec = await retriever_mod.compute_query_vector(normalized, settings)
    result = await cache_store.lookup(
        normalized, query_vec, settings=settings, sessionmaker=sessionmaker
    )
    ms = int((time.monotonic() - t0) * 1000)
    if result is not None:
        hit, cosine = result
        return hit, rr, normalized, query_vec, ms
    return None, rr, normalized, query_vec, ms


def _log_cache_outcome(hit, tier: str, lookup_ms: int, settings) -> None:
    """`$ saved` = what the avoided generation would have cost, via F2's central `estimate_cost`
    (AC-26/AC-27). The cached response carries the token counts it originally cost (AC-27b)."""
    entries = len(cache_store._CACHE._ids)
    if hit is None:
        observability.log_cache(hit=False, tier="miss", lookup_ms=lookup_ms, n_entries=entries)
        return
    saved = estimate_cost(settings.LLM_MODEL, hit.tokens_in, hit.tokens_out)
    observability.log_cache(
        hit=True, tier=tier, lookup_ms=lookup_ms, n_entries=entries,
        tokens_saved=hit.tokens_in + hit.tokens_out, est_cost_saved_usd=saved,
    )


def _replay_cached(hit: AnswerResponse, flags: PipelineFlags) -> list[SSEEvent]:
    """A cache hit's SSE shape (AC-24/AC-25). Reuses the refusal path's terminal shape — skipped
    stages, then `citations` -> `meta` -> `done` — so F14 renders a hit with no frontend change and
    the F4 latency suite's stage parser reads it for free.

    The answer is replayed as ONE `token` event carrying the full text, so `astream` and `answer`
    reassemble byte-identically (the disclaimer is already baked into the cached `answer`).
    """
    response = hit.model_copy(update={"cache_hit": True, "pipeline_flags": flags})
    return [
        stages.emit("searching", "skipped"),
        stages.emit("reranking", "skipped"),
        stages.emit("compressing", "skipped"),
        stages.emit("generating", "skipped"),
        stages.emit("citing", "skipped"),
        SSEEvent(event="token", data={"token": response.answer}),
        SSEEvent(event="citations",
                 data={"citations": [c.model_dump() for c in response.citations]}),
        SSEEvent(event="meta", data=response.model_dump(exclude={"answer"})),
        SSEEvent(event="done", data={}),
    ]


async def _pipeline_events(
    query: str,
    k: int,
    namespace: str | None,
    flags: PipelineFlags,
    memory: MemoryContext | None,
    session,
    settings,
    sessionmaker=None,
    session_id: str | None = None,
) -> AsyncIterator[SSEEvent]:
    """The single source of pipeline truth `astream`/`answer` both consume (AC-19). Emits the
    ordered `stage*` -> `token*` -> `citations` -> `meta` -> `done`|`error` sequence."""
    query = _truncate_query(query, settings)
    # F5: reflect the request/eval hybrid toggle onto settings before retrieval, so the F3→F5 seam
    # (`retriever.retrieve`, which reads the mode from settings) honours `flags.hybrid` (AC-12).
    settings = flags_mod.apply_flags(settings, flags)
    # Install this request's trace before any seam runs; `stages.emit` drains it per stage.
    trace.start(settings)

    # F9: cache lookup, between rewrite and retrieval (CLAUDE.md pipeline order). Everything is
    # gated on ENABLE_CACHE: flag off emits no `cache_lookup` stage and leaves `rr`/`query_vec`
    # None, so the path below is byte-for-byte f8-compression-after (AC-30).
    rr = None
    query_vec = None
    if settings.ENABLE_CACHE:
        sessionmaker = sessionmaker or get_sessionmaker()
        yield stages.emit("cache_lookup", "started")
        hit, rr, normalized, query_vec, lookup_ms = await _cache_lookup(
            query, memory, settings, sessionmaker
        )
        # `query_vec is None` means the exact-match Redis tier answered before any embed ran.
        trace.record("cache_lookup", {
            "hit": hit is not None,
            "tier": "redis_exact" if query_vec is None else "semantic",
            "key": trace.clip(normalized),
            "n_entries": len(cache_store._CACHE._ids),
        })
        yield stages.emit("cache_lookup", "done", ms=lookup_ms)
        _log_cache_outcome(hit, "redis" if query_vec is None else "semantic", lookup_ms, settings)
        if hit is not None:
            for ev in _replay_cached(hit, flags):
                yield ev
            return

    # F7: `rewriting` brackets the retrieval call because the rewrite runs *inside*
    # `rewrite_mod.retrieve`. Flag-driven (not result-driven) so `started` can precede the work;
    # `last_rewrite()` below only confirms it afterwards. Off → one `skipped` frame, so the UI shows
    # the stage as deliberately not-run rather than silently missing.
    if settings.ENABLE_QUERY_REWRITE:
        yield stages.emit("rewriting", "started")
    else:
        yield stages.emit("rewriting", "skipped")

    t0 = time.monotonic()
    yield stages.emit("searching", "started")
    try:
        # F7: the outer retrieval seam is now `rewrite.retrieve` (still inside the `searching`
        # stage — no new SSE stage). With `ENABLE_QUERY_REWRITE` off it delegates verbatim to
        # `retriever.retrieve` (byte-for-byte f6-rerank-after); with it on it rewrites the query
        # via gpt-4o-mini, fans out, union+RRF-merges, and single-reranks. `memory` is threaded
        # through so history-aware condensation activates automatically once F17 populates it.
        # F9 hands in the `rr` it already computed (no second rewrite) and the `query_vec` it
        # already embedded (no second embed of the normalized query) — both None when cache is off.
        chunks = await errors_mod.call_with_retry(
            lambda: rewrite_mod.retrieve(query, k, namespace, settings, memory, rr=rr,
                                         query_vec=query_vec),
            settings=settings,
        )
    except Exception as exc:
        # Any unrecoverable retrieval failure (ProviderError after retry exhaustion, or a
        # non-retryable error like the namespace-fan-out failure documented in retriever.py)
        # becomes a terminal SSE error event rather than raising past the generator boundary.
        # (Hybrid mode degrades to BM25-only instead of raising here — see hybrid.hybrid_retrieve.)
        yield SSEEvent(event="error", data={"message": str(exc)})
        return
    # F5: hybrid retrieval sets this out-of-band when it fell back to BM25-only (AC-14/AC-17);
    # dense_only/bm25_only paths never set it, so this reads False there.
    degraded = hybrid_mod.was_degraded()
    # F7: read the out-of-band rewrite result (None when rewrite was off/not run) to pass the answer
    # language EXPLICITLY into the generation prompt (AC-9). None → empty directive → the existing
    # "respond in the question's language" system-prompt rule stands unchanged.
    rewrite_result = rewrite_mod.last_rewrite()
    language_directive = prompt.render_language_directive(
        rewrite_result.language if rewrite_result else None
    )
    retrieval_ms = int((time.monotonic() - t0) * 1000)
    if settings.ENABLE_QUERY_REWRITE:
        yield stages.emit("rewriting", "done", ms=retrieval_ms if rewrite_result else None)

    # F6: rerank runs two levels below this generator (inside `rewrite.retrieve` →
    # `retriever.retrieve`), so its duration comes back out-of-band via `last_rerank_ms()` —
    # the same read-and-reset pattern as `was_degraded()` / `last_rewrite()` above. The paired
    # frames are therefore emitted just after the work rather than around it; the `ms` is the real
    # measured span either way. `searching` is reported NET of rerank so the two don't double-count.
    rerank_ms = rerank_mod.last_rerank_ms() if settings.ENABLE_RERANK else None
    yield stages.emit("searching", "done", ms=max(retrieval_ms - (rerank_ms or 0), 0))
    if rerank_ms is None:
        yield stages.emit("reranking", "skipped")
    else:
        yield stages.emit("reranking", "started")
        yield stages.emit("reranking", "done", ms=rerank_ms)

    if refusal.pre_llm_gate(chunks, settings):
        suggestions = refusal.suggestion_citations(chunks, settings.REFUSAL_SUGGESTION_COUNT)
        # Cost saved: the input tokens the skipped prompt would have cost, $0 output.
        would_be_prompt = prompt.SYSTEM_PROMPT + context.format_context(chunks) + query
        would_be_tokens_in = len(_ENC.encode(would_be_prompt))
        await observability.log_llm_cost(settings.LLM_MODEL, would_be_tokens_in, 0)
        yield stages.emit("compressing", "skipped")  # refusal short-circuits before compression
        yield stages.emit("generating", "skipped")
        yield stages.emit("citing", "skipped")
        response = AnswerResponse(
            answer="",
            citations=suggestions,
            refused=True,
            refusal_reason="low_retrieval_confidence",
            pipeline_flags=flags,
            # F17: surfaced from the request (was hardcoded None/False). memory-off callers pass
            # session_id=None and memory=None, so this stays byte-for-byte f9-cache-after (AC-16/33).
            session_id=session_id,
            memory_summarized=(memory.summarized if memory else False),
            cache_hit=False,
            # F9/AC-27b: the prompt tokens the skipped generation WOULD have cost, mirroring the
            # log_llm_cost call above. Refusals are never cached (AC-16), so this is telemetry
            # rather than a cache input.
            tokens_in=would_be_tokens_in,
            tokens_out=0,
            degraded=degraded,
        )
        yield SSEEvent(event="citations", data={"citations": [c.model_dump() for c in suggestions]})
        yield SSEEvent(event="meta", data=response.model_dump(exclude={"answer"}))
        yield SSEEvent(event="done", data={})
        return

    # F8: compress the (non-refused) reranked context before generation — CLAUDE.md order
    # `rerank → refusal gate → compress → generate`. Flag off ≡ f7-rewrite-after (no-op). Scoring
    # uses the F7 normalized query (same query F6 reranked against) when rewrite ran, else the raw
    # query. `chunks` is reassigned in place, so the SAME compressed list drives format_context, the
    # cost token-count, and parse_citations — [n] stays 1:1. Compression is called directly here
    # (unlike rerank), so it gets a genuine live `started`→`done` span around the real work.
    if settings.ENABLE_COMPRESSION:
        yield stages.emit("compressing", "started")
        timer = stages.Timer()
        scoring_query = rewrite_result.normalized if rewrite_result else query
        chunks = await compression_mod.compress_chunks(scoring_query, chunks, settings)
        yield stages.emit("compressing", "done", ms=timer.ms())
    else:
        yield stages.emit("compressing", "skipped")

    gen_timer = stages.Timer()
    yield stages.emit("generating", "started")
    memory_block = prompt.render_memory_block(memory)
    chain_input = {"chunks": chunks, "memory_block": memory_block, "question": query,
                   "language_directive": language_directive}
    llm = build_llm(settings)
    chain = build_generate_chain(llm)
    handler = observability.langfuse_handler(session_id=session_id, settings=settings)
    config = {"callbacks": [handler]} if handler else {}

    answer_text = ""
    tokens_out = 0
    try:
        async for event in _stream_chain_with_retry(chain, chain_input, config, settings):
            if event["event"] == "on_chat_model_stream":
                token = event["data"]["chunk"].content
                if token:
                    answer_text += token
                    tokens_out += 1
                    yield SSEEvent(event="token", data={"token": token})
    except Exception as exc:
        yield SSEEvent(event="error", data={"message": str(exc)})
        return
    # The context the answer was actually written from — the end of the funnel the earlier stages
    # narrowed, and what every `[n]` in the answer points at.
    trace.record("generating", {
        "model": settings.LLM_MODEL,
        "tokens_out": tokens_out,
        "memory_used": bool(memory_block),
        "context": trace.chunk_rows(chunks, "rerank_score"),
    })
    yield stages.emit("generating", "done", ms=gen_timer.ms())

    full_prompt = (prompt.SYSTEM_PROMPT + language_directive + context.format_context(chunks)
                   + memory_block + query)
    tokens_in = len(_ENC.encode(full_prompt))
    await observability.log_llm_cost(settings.LLM_MODEL, tokens_in, tokens_out)

    yield stages.emit("citing", "started")
    cite_timer = stages.Timer()
    resolved_citations = await citations_mod.parse_citations(answer_text, chunks, session)
    refused = refusal.post_llm_gate(resolved_citations)
    yield stages.emit("citing", "done", ms=cite_timer.ms())

    if not refused:
        # AC-14: appended as its own trailing token event (not just baked into the internal
        # AnswerResponse) so `answer()`'s token-reconstruction and `astream()`'s live stream
        # agree on the exact same final text — `meta` never carries `answer` (see docstring).
        disclaimer_suffix = f"\n\n{settings.DISCLAIMER_TEXT}"
        answer_text += disclaimer_suffix
        yield SSEEvent(event="token", data={"token": disclaimer_suffix})

    response = AnswerResponse(
        answer=answer_text,
        citations=resolved_citations,
        refused=refused,
        refusal_reason="no_grounded_claims" if refused else None,
        pipeline_flags=flags,
        session_id=session_id,
        memory_summarized=(memory.summarized if memory else False),
        cache_hit=False,
        # F9/AC-27b: surfaced rather than discarded. These are the counts already computed above
        # for log_llm_cost; carrying them on the response is what lets a later cache HIT report the
        # spend it avoided, and what lets the F4 latency suite read `cache_cost_saved_mean` off the
        # SSE `meta` event with no extra plumbing.
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        degraded=degraded,
    )
    yield SSEEvent(event="citations",
                   data={"citations": [c.model_dump() for c in resolved_citations]})
    yield SSEEvent(event="meta", data=response.model_dump(exclude={"answer"}))
    yield SSEEvent(event="done", data={})

    # F9 write-behind (AC-14/AC-15): AFTER the terminal `done`, so the cache write can never add
    # latency to the response — the entire reason this is a task and not an await.
    #
    # Only clean answers are cached (AC-16): a refusal is not an answer, a `degraded` answer came
    # from BM25-only and would be frozen in at full confidence, and a zero-citation answer failed
    # the post-LLM gate. Caching any of those would preserve a bad answer for 24h.
    if settings.ENABLE_CACHE and query_vec is not None and not refused and not degraded \
            and resolved_citations:
        cache_store.schedule_write(
            keys.normalize(rr.normalized if rr else query), query_vec, response,
            settings=settings, sessionmaker=sessionmaker or get_sessionmaker(),
        )


async def astream(
    query: str,
    k: int = 5,
    namespace: str | None = None,
    flags: PipelineFlags | None = None,
    memory: MemoryContext | None = None,
    *,
    session,
    settings,
    sessionmaker=None,
    session_id: str | None = None,
) -> AsyncIterator[SSEEvent]:
    """`sessionmaker` is F9's: the cache opens its OWN short-lived sessions for lookup and for the
    write-behind task, which outlives the request's `session`. Defaults to the app-wide one; tests
    and the F4 harness inject theirs. Unused when `ENABLE_CACHE` is off.

    `session_id` is F17's: threaded onto the response + Langfuse span when memory is active; `None`
    for stateless/harness calls, keeping the path byte-for-byte f9-cache-after (AC-16/33)."""
    async for ev in _pipeline_events(query, k, namespace, flags or PipelineFlags(), memory,
                                      session, settings, sessionmaker, session_id=session_id):
        yield ev


async def answer(
    query: str,
    k: int = 5,
    namespace: str | None = None,
    flags: PipelineFlags | None = None,
    memory: MemoryContext | None = None,
    *,
    session,
    settings,
    sessionmaker=None,
    session_id: str | None = None,
) -> AnswerResponse:
    """Collects `_pipeline_events` into the terminal `meta` event's `AnswerResponse` (AC-20) —
    `answer` text is reassembled from the accumulated `token` events since `meta` omits it (SSE
    contract: "meta = final AnswerResponse sans answer text").

    A cache hit replays its answer as a single `token` event, so this reassembly is byte-identical
    on hit and miss (AC-25)."""
    full_answer_text = ""
    meta_payload = None
    async for ev in astream(query, k, namespace, flags, memory, session=session, settings=settings,
                            sessionmaker=sessionmaker, session_id=session_id):
        if ev.event == "token":
            full_answer_text += ev.data["token"]
        elif ev.event == "meta":
            meta_payload = ev.data
        elif ev.event == "error":
            raise errors_mod.ProviderError(ev.data.get("message", "pipeline error"))
    if meta_payload is None:
        raise errors_mod.ProviderError("pipeline ended without a meta event")
    return AnswerResponse(answer=full_answer_text, **meta_payload)
