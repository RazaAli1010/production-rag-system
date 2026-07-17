"""F9 (T8): `compute_query_vector` + the `query_vec` thread through the retrieval seam.

Two properties, and the first matters more than the second:

1. **`query_vec=None` is byte-for-byte the pre-F9 path.** Every retrieval function grew an optional
   parameter; with it absent, the by-query surface is used exactly as before. This is what keeps
   `f8-compression-after`'s numbers comparable and what makes `ENABLE_CACHE=false` a true no-op.
2. **`query_vec` supplied skips the embed.** The by-vector surface is called with that exact
   vector, once per namespace, and the by-query surface is never touched — so a cache MISS costs
   fewer embeds than the old path rather than more (design §2). A regression here shows up at the
   gate as a miss-path latency regression, which is a gate failure.

Pinecone/OpenAI are injected fakes (F2's DI style), never real.
"""

import pytest

from app.core.settings import Settings
from app.rag import hybrid, retriever


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


class FakeStore:
    """Records which similarity surface was used and with what."""

    def __init__(self):
        self.by_query: list[tuple] = []
        self.by_vector: list[tuple] = []

    def _hit(self):
        from langchain_core.documents import Document

        doc = Document(id="d:0", page_content="body", metadata={"doc_id": "d", "title": "T"})
        return [(doc, 0.9)]

    async def asimilarity_search_with_score(self, query, k, namespace):
        self.by_query.append((query, k, namespace))
        return self._hit()

    async def asimilarity_search_by_vector_with_score(self, vector, k, namespace):
        self.by_vector.append((tuple(vector), k, namespace))
        return self._hit()


@pytest.fixture
def fake_store(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(retriever, "_build_store", lambda settings: store)
    return store


# --------------------------------------------------------------- parity: no vector (AC-13)

async def test_no_query_vec_uses_the_by_query_surface(fake_store):
    """The parity guarantee: absent a vector, F9 is invisible."""
    await retriever.dense_retrieve("probation rules", 5, "pu", _settings())

    assert fake_store.by_query == [("probation rules", 5, "pu")]
    assert fake_store.by_vector == []


async def test_no_query_vec_across_the_namespace_fanout(fake_store):
    await retriever.dense_retrieve("probation rules", 5, None, _settings())

    assert [ns for _, _, ns in fake_store.by_query] == ["pu", "hec"]
    assert fake_store.by_vector == []


# --------------------------------------------------------------- reuse: vector supplied (AC-11)

async def test_query_vec_uses_the_by_vector_surface_and_never_embeds(fake_store):
    vec = [0.1, 0.2, 0.3]
    await retriever.dense_retrieve("probation rules", 5, "pu", _settings(), vec)

    assert fake_store.by_vector == [((0.1, 0.2, 0.3), 5, "pu")]
    assert fake_store.by_query == [], "the by-query surface would embed again — the whole point"


async def test_one_vector_serves_the_whole_namespace_fanout(fake_store):
    """2 namespaces, 1 vector, 0 embeds — where the miss-path saving actually comes from."""
    vec = [0.1, 0.2, 0.3]
    await retriever.dense_retrieve("probation rules", 5, None, _settings(), vec)

    assert len(fake_store.by_vector) == 2
    assert [ns for _, _, ns in fake_store.by_vector] == ["pu", "hec"]
    assert {v for v, _, _ in fake_store.by_vector} == {(0.1, 0.2, 0.3)}
    assert fake_store.by_query == []


# --------------------------------------------------------------- through the seams

async def test_query_vec_threads_through_gather_candidate_pool_dense_only(fake_store):
    vec = [0.4, 0.5]
    await retriever.gather_candidate_pool("q", 5, "pu", _settings(ENABLE_HYBRID=False), vec)

    assert fake_store.by_vector and not fake_store.by_query


async def test_query_vec_threads_through_retrieve_seam(fake_store):
    vec = [0.4, 0.5]
    await retriever.retrieve("q", 5, "pu", _settings(ENABLE_HYBRID=False, ENABLE_RERANK=False),
                             vec)

    assert fake_store.by_vector and not fake_store.by_query


async def test_query_vec_threads_through_hybrid_dense_half(fake_store, monkeypatch):
    """The path the shipped config actually takes — hybrid is ON at the F9 gate, so if the vector
    stopped here the reuse would never happen in production."""
    async def _no_bm25(settings):
        return None

    monkeypatch.setattr(hybrid, "load_bm25", _no_bm25)
    monkeypatch.setattr(hybrid, "sparse_scores", lambda *a, **k: [])

    async def _no_hydrate(ids, namespace, settings):
        return []

    monkeypatch.setattr(hybrid, "hydrate_sparse_only", _no_hydrate)

    vec = [0.7, 0.8]
    await hybrid.hybrid_retrieve("q", 5, "pu", _settings(ENABLE_HYBRID=True), vec)

    assert fake_store.by_vector == [((0.7, 0.8), _settings().HYBRID_DENSE_TOP_K, "pu")]
    assert fake_store.by_query == []


# --------------------------------------------------------------- embed_query itself (AC-5)

async def test_compute_query_vector_uses_the_async_surface(monkeypatch):
    calls = []

    class FakeEmbeddings:
        async def aembed_query(self, text):
            calls.append(text)
            return [0.1] * 1536

    monkeypatch.setattr(retriever, "_build_embeddings", lambda settings: FakeEmbeddings())

    vec = await retriever.compute_query_vector("probation rules", _settings())

    assert calls == ["probation rules"]
    assert len(vec) == 1536


async def test_compute_query_vector_is_configured_from_f2_embed_settings(monkeypatch):
    """The cache must embed with the SAME model the corpus was indexed with, or the query vector
    and the cached vectors are not comparable."""
    captured = {}

    def _fake_openai_embeddings(model, api_key):
        captured["model"] = model
        return object()

    import app.rag.retriever as r

    monkeypatch.setattr(
        "langchain_openai.OpenAIEmbeddings",
        lambda model, api_key: _fake_openai_embeddings(model, api_key),
    )
    r._build_embeddings(_settings())

    assert captured["model"] == "text-embedding-3-small"
