"""De-risking test for design.md §2's central claim: `PineconeVectorStore.
asimilarity_search_with_score` runs fully async (no thread-pool sync-wrap) when constructed with
an `_IndexAsyncio` instance, exactly as F3's `retriever._build_store` constructs it. There was no
in-repo precedent for `PineconeVectorStore` before F3 (F2 uses the raw `IndexAsyncio` client
directly), so this claim is verified here rather than assumed.
"""

import asyncio
import types

from langchain_pinecone import PineconeVectorStore
from pinecone.db_data.index_asyncio import _IndexAsyncio


class FakeIndexAsyncio(_IndexAsyncio):
    """A real `_IndexAsyncio` subclass (so `isinstance(..., _IndexAsyncio)` is True, matching
    `PineconeVectorStore`'s own `_async_index_provided` check) with `__init__`/`query` overridden
    so no real Pinecone connection is ever attempted."""

    def __init__(self):
        self.query_calls = 0
        # PineconeVectorStore.__init__ reads config.host/.api_key when an index is provided
        self.config = types.SimpleNamespace(host="fake-host", api_key="fake-key")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def query(self, **kwargs):
        self.query_calls += 1
        return {
            "matches": [
                {"id": "doc:0", "score": 0.9, "metadata": {"text": "body", "doc_id": "doc",
                                                            "title": "T"}},
            ]
        }


class FakeEmbeddings:
    async def aembed_query(self, text):
        return [0.1, 0.2, 0.3]


async def test_asimilarity_search_with_score_never_touches_a_thread_executor(monkeypatch):
    fake_index = FakeIndexAsyncio()
    store = PineconeVectorStore(index=fake_index, embedding=FakeEmbeddings(), text_key="text")
    assert store._async_index_provided is True

    def _forbidden(*a, **kw):
        raise AssertionError("run_in_executor called — asimilarity_search_with_score is not "
                             "fully async for an injected _IndexAsyncio")

    loop = asyncio.get_event_loop()
    monkeypatch.setattr(loop, "run_in_executor", _forbidden)

    docs_and_scores = await store.asimilarity_search_with_score("q", k=1, namespace="pu")

    assert fake_index.query_calls == 1
    assert len(docs_and_scores) == 1
    doc, score = docs_and_scores[0]
    assert doc.id == "doc:0"
    assert doc.page_content == "body"
    assert score == 0.9
