import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult

import app.db.models.corpus as corpus
from app.core.contracts import RetrievedChunk
from app.core.settings import Settings
from app.rag import baseline, errors, retriever

FULL_ANSWER = "According to policy [1], students must comply."


def _settings(**o):
    return Settings(
        _env_file=None,
        DATABASE_URL="postgresql+asyncpg://u:p@localhost:5432/db",
        ADMIN_EMAIL="a@b.c",
        ADMIN_PASSWORD="x",
        OPENAI_API_KEY="k",
        PINECONE_API_KEY="k",
        PINECONE_INDEX="i",
        **o,
    )


async def _seed_one_chunk(session):
    doc = corpus.Document(doc_id="d", title="Doc Title", source_org="PU", url="http://x",
                          file_type="pdf", version_label="v1", is_scanned=False, page_count=10)
    session.add(doc)
    await session.flush()
    session.add(corpus.Chunk(chunk_id="d:0", doc_id="d", seq=0, text="chunk body text",
                             section_heading="S", page_start=1, page_end=1, anchor=None,
                             token_count=3))
    await session.flush()


def _retrieved_chunk(score=0.9):
    return RetrievedChunk(chunk_id="d:0", doc_id="d", title="Doc Title", text="chunk body text",
                          dense_score=score)


async def _fake_retrieve_high_score(*a, **kw):
    return [_retrieved_chunk(0.9)]


async def _fake_retrieve_low_score(*a, **kw):
    return [_retrieved_chunk(0.01)]


async def _fake_retrieve_empty(*a, **kw):
    return []


class MidStreamFailureChatModel(BaseChatModel):
    @property
    def _llm_type(self):
        return "fake-mid-stream-failure"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="partial"))])

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        yield ChatGenerationChunk(message=AIMessageChunk(content="partial "))
        raise RuntimeError("mid-stream boom")


class FlakyThenSucceedsChatModel(BaseChatModel):
    attempts: int = 0

    @property
    def _llm_type(self):
        return "flaky-then-succeeds"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="ok"))])

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        self.attempts += 1
        if self.attempts == 1:
            err = Exception("rate limited")
            err.status_code = 429
            raise err
        yield ChatGenerationChunk(message=AIMessageChunk(content="ok"))


class AlwaysFailsChatModel(BaseChatModel):
    attempts: int = 0

    @property
    def _llm_type(self):
        return "always-fails"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="ok"))])

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        self.attempts += 1
        err = Exception("rate limited")
        err.status_code = 429
        raise err
        yield  # pragma: no cover — makes this an async generator


def _fake_llm(text):
    """Returns a `build_llm`-shaped factory yielding a fresh `GenericFakeChatModel` per call
    (each `_pipeline_events` run needs its own un-exhausted `messages` iterator)."""
    return lambda settings: GenericFakeChatModel(messages=iter([AIMessage(content=text)]))


async def _collect(agen):
    return [ev async for ev in agen]


async def test_astream_happy_path_event_order_and_content(monkeypatch, session):
    await _seed_one_chunk(session)
    monkeypatch.setattr(retriever, "retrieve", _fake_retrieve_high_score)
    monkeypatch.setattr(baseline, "build_llm", _fake_llm(FULL_ANSWER))
    settings = _settings()

    events = await _collect(
        baseline.astream("what is the policy?", session=session, settings=settings)
    )
    kinds = [e.event for e in events]

    assert kinds[0] == "stage"
    assert events[0].data == {"stage": "searching", "status": "started", "ms": None}
    assert kinds[-1] == "done"
    assert kinds[-2] == "meta"
    assert kinds[-3] == "citations"
    assert "token" in kinds
    assert kinds.index("citations") > kinds.index("token")
    assert kinds.index("meta") > kinds.index("citations")

    meta = next(e for e in events if e.event == "meta")
    assert meta.data["refused"] is False
    assert meta.data["refusal_reason"] is None
    assert len(meta.data["citations"]) == 1
    assert meta.data["citations"][0]["chunk_id"] == "d:0"

    reassembled = "".join(e.data["token"] for e in events if e.event == "token")
    assert reassembled == FULL_ANSWER + f"\n\n{settings.DISCLAIMER_TEXT}"


async def test_astream_and_answer_agree_on_terminal_fields(monkeypatch, session):
    await _seed_one_chunk(session)
    monkeypatch.setattr(retriever, "retrieve", _fake_retrieve_high_score)
    monkeypatch.setattr(baseline, "build_llm", _fake_llm(FULL_ANSWER))
    settings = _settings()

    events = await _collect(baseline.astream("q", session=session, settings=settings))
    reassembled = "".join(e.data["token"] for e in events if e.event == "token")
    meta = next(e for e in events if e.event == "meta").data

    direct = await baseline.answer("q", session=session, settings=settings)

    assert direct.answer == reassembled
    assert reassembled == FULL_ANSWER + f"\n\n{settings.DISCLAIMER_TEXT}"
    assert [c.model_dump() for c in direct.citations] == meta["citations"]
    assert direct.refused == meta["refused"]


async def test_pre_llm_refusal_skips_llm_and_marks_stages_skipped(monkeypatch, session):
    monkeypatch.setattr(retriever, "retrieve", _fake_retrieve_low_score)
    llm_calls = []
    monkeypatch.setattr(baseline, "build_llm", lambda settings: llm_calls.append(1))
    settings = _settings()

    events = await _collect(
        baseline.astream("irrelevant question", session=session, settings=settings)
    )

    assert llm_calls == []  # LLM never constructed/called
    stage_events = [e for e in events if e.event == "stage"]
    gen_stage = next(e for e in stage_events if e.data["stage"] == "generating")
    cite_stage = next(e for e in stage_events if e.data["stage"] == "citing")
    assert gen_stage.data["status"] == "skipped"
    assert cite_stage.data["status"] == "skipped"

    meta = next(e for e in events if e.event == "meta").data
    assert meta["refused"] is True
    assert meta["refusal_reason"] == "low_retrieval_confidence"
    assert len(meta["citations"]) <= settings.REFUSAL_SUGGESTION_COUNT
    assert not any(e.event == "token" for e in events)


async def test_pre_llm_refusal_fires_on_empty_retrieval(monkeypatch, session):
    monkeypatch.setattr(retriever, "retrieve", _fake_retrieve_empty)
    settings = _settings()
    events = await _collect(baseline.astream("q", session=session, settings=settings))
    meta = next(e for e in events if e.event == "meta").data
    assert meta["refused"] is True
    assert meta["citations"] == []


async def test_zero_citation_answer_converts_to_refusal(monkeypatch, session):
    await _seed_one_chunk(session)
    monkeypatch.setattr(retriever, "retrieve", _fake_retrieve_high_score)
    no_citation_answer = "This answer cites nothing at all."
    monkeypatch.setattr(baseline, "build_llm", _fake_llm(no_citation_answer))
    settings = _settings()

    events = await _collect(baseline.astream("q", session=session, settings=settings))
    meta = next(e for e in events if e.event == "meta").data

    assert meta["refused"] is True
    assert meta["refusal_reason"] == "no_grounded_claims"
    # tokens WERE streamed (this is a post-LLM refusal, not pre-LLM)
    assert any(e.event == "token" for e in events)


async def test_mid_stream_failure_yields_terminal_error_event(monkeypatch, session):
    await _seed_one_chunk(session)
    monkeypatch.setattr(retriever, "retrieve", _fake_retrieve_high_score)
    monkeypatch.setattr(baseline, "build_llm", lambda settings: MidStreamFailureChatModel())
    settings = _settings()

    events = await _collect(baseline.astream("q", session=session, settings=settings))

    assert events[-1].event == "error"
    assert any(e.event == "token" for e in events)  # at least one token got out before the failure
    assert not any(e.event == "meta" for e in events)
    assert not any(e.event == "done" for e in events)


async def test_query_truncated_before_retrieval(monkeypatch, session):
    seen_queries = []

    async def _spy_retrieve(query, k, namespace, settings, query_vec=None):
        seen_queries.append(query)
        return [_retrieved_chunk(0.9)]

    monkeypatch.setattr(retriever, "retrieve", _spy_retrieve)
    monkeypatch.setattr(baseline, "build_llm", _fake_llm("ok [1]."))
    settings = _settings(MAX_QUERY_TOKENS=5)
    await _seed_one_chunk(session)

    long_query = " ".join(f"word{i}" for i in range(50))
    await _collect(baseline.astream(long_query, session=session, settings=settings))

    assert seen_queries[0] != long_query
    assert len(baseline._ENC.encode(seen_queries[0])) <= 5


async def test_stream_chain_with_retry_retries_before_first_token():
    llm = FlakyThenSucceedsChatModel()
    chain = baseline.build_generate_chain(llm)
    chunk_input = {"chunks": [_retrieved_chunk()], "memory_block": "", "question": "q",
                   "language_directive": ""}
    settings = _settings(LLM_MAX_RETRIES=2)

    tokens = []
    async for event in baseline._stream_chain_with_retry(chain, chunk_input, {}, settings):
        if event["event"] == "on_chat_model_stream":
            tokens.append(event["data"]["chunk"].content)

    assert "".join(tokens) == "ok"
    assert llm.attempts == 2  # failed once, succeeded on retry


async def test_stream_chain_with_retry_raises_provider_error_on_exhaustion():
    llm = AlwaysFailsChatModel()
    chain = baseline.build_generate_chain(llm)
    chunk_input = {"chunks": [_retrieved_chunk()], "memory_block": "", "question": "q",
                   "language_directive": ""}
    settings = _settings(LLM_MAX_RETRIES=2)

    with pytest.raises(errors.ProviderError):
        async for _ in baseline._stream_chain_with_retry(chain, chunk_input, {}, settings):
            pass

    assert llm.attempts == 3  # initial + 2 retries
