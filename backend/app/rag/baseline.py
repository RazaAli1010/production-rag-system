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

from app.core.contracts import AnswerResponse, MemoryContext
from app.rag import citations as citations_mod
from app.rag import context, observability, prompt, refusal
from app.rag import errors as errors_mod
from app.rag import flags as flags_mod
from app.rag import hybrid as hybrid_mod
from app.rag import retriever as retriever_mod
from app.rag.events import SSEEvent, stage_event
from app.rag.schemas import PipelineFlags

logger = structlog.get_logger(__name__)

# Mirrors app.indexing.chunkers.base's tiktoken cl100k_base truncate pattern, bound to
# MAX_QUERY_TOKENS instead of EMBED_MAX_CHUNK_TOKENS (AC-13) — a different Settings key, so it
# isn't the literal same function, but the same encode/decode/warn shape.
_ENC = tiktoken.get_encoding("cl100k_base")


def build_llm(settings):
    return ChatOpenAI(model=settings.LLM_MODEL, temperature=0)


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


async def _pipeline_events(
    query: str,
    k: int,
    namespace: str | None,
    flags: PipelineFlags,
    memory: MemoryContext | None,
    session,
    settings,
) -> AsyncIterator[SSEEvent]:
    """The single source of pipeline truth `astream`/`answer` both consume (AC-19). Emits the
    ordered `stage*` -> `token*` -> `citations` -> `meta` -> `done`|`error` sequence."""
    query = _truncate_query(query, settings)
    # F5: reflect the request/eval hybrid toggle onto settings before retrieval, so the F3→F5 seam
    # (`retriever.retrieve`, which reads the mode from settings) honours `flags.hybrid` (AC-12).
    settings = flags_mod.apply_flags(settings, flags)

    t0 = time.monotonic()
    yield stage_event("searching", "started")
    try:
        chunks = await errors_mod.call_with_retry(
            lambda: retriever_mod.retrieve(query, k, namespace, settings), settings=settings
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
    yield stage_event("searching", "done", ms=int((time.monotonic() - t0) * 1000))

    if refusal.pre_llm_gate(chunks, settings):
        suggestions = refusal.suggestion_citations(chunks, settings.REFUSAL_SUGGESTION_COUNT)
        # Cost saved: the input tokens the skipped prompt would have cost, $0 output.
        would_be_prompt = prompt.SYSTEM_PROMPT + context.format_context(chunks) + query
        would_be_tokens_in = len(_ENC.encode(would_be_prompt))
        await observability.log_llm_cost(settings.LLM_MODEL, would_be_tokens_in, 0)
        yield stage_event("generating", "skipped")
        yield stage_event("citing", "skipped")
        response = AnswerResponse(
            answer="",
            citations=suggestions,
            refused=True,
            refusal_reason="low_retrieval_confidence",
            pipeline_flags=flags,
            session_id=None,
            memory_summarized=False,
            cache_hit=False,
            degraded=degraded,
        )
        yield SSEEvent(event="citations", data={"citations": [c.model_dump() for c in suggestions]})
        yield SSEEvent(event="meta", data=response.model_dump(exclude={"answer"}))
        yield SSEEvent(event="done", data={})
        return

    yield stage_event("generating", "started")
    memory_block = prompt.render_memory_block(memory)
    chain_input = {"chunks": chunks, "memory_block": memory_block, "question": query}
    llm = build_llm(settings)
    chain = build_generate_chain(llm)
    handler = observability.langfuse_handler(session_id=None, settings=settings)
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
    yield stage_event("generating", "done")

    full_prompt = prompt.SYSTEM_PROMPT + context.format_context(chunks) + memory_block + query
    tokens_in = len(_ENC.encode(full_prompt))
    await observability.log_llm_cost(settings.LLM_MODEL, tokens_in, tokens_out)

    yield stage_event("citing", "started")
    resolved_citations = await citations_mod.parse_citations(answer_text, chunks, session)
    refused = refusal.post_llm_gate(resolved_citations)
    yield stage_event("citing", "done")

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
        session_id=None,
        memory_summarized=False,
        cache_hit=False,
        degraded=degraded,
    )
    yield SSEEvent(event="citations",
                   data={"citations": [c.model_dump() for c in resolved_citations]})
    yield SSEEvent(event="meta", data=response.model_dump(exclude={"answer"}))
    yield SSEEvent(event="done", data={})


async def astream(
    query: str,
    k: int = 5,
    namespace: str | None = None,
    flags: PipelineFlags | None = None,
    memory: MemoryContext | None = None,
    *,
    session,
    settings,
) -> AsyncIterator[SSEEvent]:
    async for ev in _pipeline_events(query, k, namespace, flags or PipelineFlags(), memory,
                                      session, settings):
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
) -> AnswerResponse:
    """Collects `_pipeline_events` into the terminal `meta` event's `AnswerResponse` (AC-20) —
    `answer` text is reassembled from the accumulated `token` events since `meta` omits it (SSE
    contract: "meta = final AnswerResponse sans answer text")."""
    full_answer_text = ""
    meta_payload = None
    async for ev in astream(query, k, namespace, flags, memory, session=session, settings=settings):
        if ev.event == "token":
            full_answer_text += ev.data["token"]
        elif ev.event == "meta":
            meta_payload = ev.data
        elif ev.event == "error":
            raise errors_mod.ProviderError(ev.data.get("message", "pipeline error"))
    if meta_payload is None:
        raise errors_mod.ProviderError("pipeline ended without a meta event")
    return AnswerResponse(answer=full_answer_text, **meta_payload)
