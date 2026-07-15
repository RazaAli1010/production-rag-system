
import pytest
from langchain_core.documents import Document

from app.core.settings import Settings
from app.rag import retriever


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


def _doc(chunk_id, doc_id="d", title="Title", text="body", **md_overrides):
    md = {"doc_id": doc_id, "title": title, "section_heading": "H", "page_start": 1,
          "page_end": 1, "anchor": "", "token_count": 2, **md_overrides}
    return Document(id=chunk_id, page_content=text, metadata=md)


class FakeStore:
    """Injected in place of `PineconeVectorStore` — mirrors the fully-async
    `asimilarity_search_with_score(query, k, namespace)` surface used by `retriever.retrieve`."""

    def __init__(self, by_namespace):
        self.by_namespace = by_namespace
        self.calls = []

    async def asimilarity_search_with_score(self, query, k, namespace=None, **kwargs):
        self.calls.append((query, k, namespace))
        pairs = self.by_namespace.get(namespace, [])
        return pairs[:k]


async def test_single_namespace_returns_dense_score_only(monkeypatch):
    store = FakeStore({"pu": [(_doc("d:0"), 0.9), (_doc("d:1"), 0.8)]})
    monkeypatch.setattr(retriever, "_build_store", lambda settings: store)

    chunks = await retriever.retrieve("q", k=5, namespace="pu", settings=_settings())

    assert [c.chunk_id for c in chunks] == ["d:0", "d:1"]
    assert [c.dense_score for c in chunks] == [0.9, 0.8]
    assert all(c.sparse_score is None and c.fused_score is None and c.rerank_score is None
               for c in chunks)
    assert store.calls == [("q", 5, "pu")]


async def test_namespace_none_fans_out_and_merges_by_score(monkeypatch):
    store = FakeStore({
        "pu": [(_doc("pu:0", doc_id="pu-doc"), 0.95), (_doc("pu:1", doc_id="pu-doc"), 0.5)],
        "hec": [(_doc("hec:0", doc_id="hec-doc"), 0.7), (_doc("hec:1", doc_id="hec-doc"), 0.6)],
    })
    monkeypatch.setattr(retriever, "_build_store", lambda settings: store)
    settings = _settings(RETRIEVAL_NAMESPACES=["pu", "hec"])

    chunks = await retriever.retrieve("q", k=3, namespace=None, settings=settings)

    assert [c.chunk_id for c in chunks] == ["pu:0", "hec:0", "hec:1"]
    assert {ns for _, _, ns in store.calls} == {"pu", "hec"}


async def test_namespace_error_surfaces_not_silently_dropped(monkeypatch):
    class FailingStore(FakeStore):
        async def asimilarity_search_with_score(self, query, k, namespace=None, **kwargs):
            if namespace == "hec":
                raise RuntimeError("hec namespace unavailable")
            return await super().asimilarity_search_with_score(query, k, namespace)

    store = FailingStore({"pu": [(_doc("pu:0"), 0.9)]})
    monkeypatch.setattr(retriever, "_build_store", lambda settings: store)
    settings = _settings(RETRIEVAL_NAMESPACES=["pu", "hec"])

    with pytest.raises(RuntimeError, match="hec namespace unavailable"):
        await retriever.retrieve("q", k=5, namespace=None, settings=settings)


def test_merge_top_k_orders_desc_and_truncates():
    from app.core.contracts import RetrievedChunk

    def rc(chunk_id, score):
        return RetrievedChunk(chunk_id=chunk_id, doc_id="d", title="T", text="x",
                              dense_score=score)

    a = [rc("a", 0.5), rc("b", 0.9)]
    b = [rc("c", 0.7)]
    merged = retriever._merge_top_k(a, b, k=2)
    assert [c.chunk_id for c in merged] == ["b", "c"]


async def test_gather_candidate_pool_matches_retrieve_when_rerank_off(monkeypatch):
    # F7 refactor regression: retrieve() == gather_candidate_pool()[:k] on the dense_only path with
    # rerank off (byte-for-byte f6-rerank-after — the pool-gathering was factored out unchanged).
    store = FakeStore({"pu": [(_doc("d:0"), 0.9), (_doc("d:1"), 0.8), (_doc("d:2"), 0.7)]})
    monkeypatch.setattr(retriever, "_build_store", lambda settings: store)
    settings = _settings()

    pool = await retriever.gather_candidate_pool("q", k=2, namespace="pu", settings=settings)
    got = await retriever.retrieve("q", k=2, namespace="pu", settings=settings)

    # retrieve() == gather_candidate_pool()[:k]; on the dense path the store already applies k, so
    # both yield the same top-2 in the same order (the refactor changed no behaviour).
    assert [c.chunk_id for c in got] == [c.chunk_id for c in pool][:2] == ["d:0", "d:1"]


def test_page_sentinel_and_empty_string_normalized_to_none():
    doc = _doc("d:0", section_heading="", page_start=-1, page_end=-1, anchor="")
    rc = retriever._to_retrieved_chunk(doc, 0.5)
    assert rc.section_heading is None
    assert rc.page_start is None
    assert rc.page_end is None
    assert rc.anchor is None
