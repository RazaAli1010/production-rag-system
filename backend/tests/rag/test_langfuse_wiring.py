"""T15: a Langfuse callback, when configured, is actually attached to the chain invocation
config — not just constructible in isolation (covered by test_observability.py)."""

import sys
import types

from langchain_core.callbacks.base import BaseCallbackHandler
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

import app.db.models.corpus as corpus
from app.core.contracts import RetrievedChunk
from app.core.settings import Settings
from app.rag import baseline, retriever


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


async def _fake_retrieve(*a, **kw):
    return [RetrievedChunk(chunk_id="d:0", doc_id="d", title="Doc Title", text="chunk body text",
                           dense_score=0.9)]


class FakeCallbackHandler(BaseCallbackHandler):
    """A real `BaseCallbackHandler` subclass (langchain checks for attributes like `run_inline`
    that a plain duck-typed object wouldn't have) standing in for `langfuse.callback.
    CallbackHandler` so this test doesn't need the real langfuse package installed."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs


async def test_langfuse_callback_attached_to_astream_events_config(monkeypatch, session):
    fake_pkg = types.ModuleType("langfuse")
    fake_callback_mod = types.ModuleType("langfuse.callback")
    fake_callback_mod.CallbackHandler = FakeCallbackHandler
    monkeypatch.setitem(sys.modules, "langfuse", fake_pkg)
    monkeypatch.setitem(sys.modules, "langfuse.callback", fake_callback_mod)

    await _seed_one_chunk(session)
    monkeypatch.setattr(retriever, "retrieve", _fake_retrieve)
    monkeypatch.setattr(
        baseline, "build_llm",
        lambda settings: GenericFakeChatModel(messages=iter([AIMessage(content="ok [1].")])),
    )

    # `RunnableSequence` is a pydantic model that rejects arbitrary instance attribute
    # assignment, so we can't wrap `chain.astream_events` directly — spy one layer up, on the
    # module-level function `_pipeline_events` calls, which receives the exact `config` dict
    # that would otherwise be threaded into `chain.astream_events(..., config=config)`.
    seen_configs = []
    real_stream_with_retry = baseline._stream_chain_with_retry

    async def _spy_stream_chain_with_retry(chain, chain_input, config, settings):
        seen_configs.append(config)
        async for event in real_stream_with_retry(chain, chain_input, config, settings):
            yield event

    monkeypatch.setattr(baseline, "_stream_chain_with_retry", _spy_stream_chain_with_retry)

    settings = _settings(LANGFUSE_PUBLIC_KEY="pub", LANGFUSE_SECRET_KEY="sec")
    events = [ev async for ev in baseline.astream("q", session=session, settings=settings)]

    assert any(ev.event == "done" for ev in events)
    assert len(seen_configs) == 1
    callbacks = seen_configs[0]["callbacks"]
    assert len(callbacks) == 1
    assert isinstance(callbacks[0], FakeCallbackHandler)


async def test_no_callbacks_attached_when_langfuse_not_configured(monkeypatch, session):
    await _seed_one_chunk(session)
    monkeypatch.setattr(retriever, "retrieve", _fake_retrieve)
    monkeypatch.setattr(
        baseline, "build_llm",
        lambda settings: GenericFakeChatModel(messages=iter([AIMessage(content="ok [1].")])),
    )

    seen_configs = []
    real_stream_with_retry = baseline._stream_chain_with_retry

    async def _spy_stream_chain_with_retry(chain, chain_input, config, settings):
        seen_configs.append(config)
        async for event in real_stream_with_retry(chain, chain_input, config, settings):
            yield event

    monkeypatch.setattr(baseline, "_stream_chain_with_retry", _spy_stream_chain_with_retry)

    settings = _settings()  # no Langfuse keys
    [ev async for ev in baseline.astream("q", session=session, settings=settings)]

    assert len(seen_configs) == 1
    assert seen_configs[0].get("callbacks", []) == []
