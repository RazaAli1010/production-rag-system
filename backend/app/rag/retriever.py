"""Dense retrieval — the F3→F5 seam (design.md §2, §5).

`retrieve(query, k, namespace, settings)` is a plain async callable (not a `BaseRetriever`
subclass) with a signature F5 swaps the *body* of but never the shape: `(query, k, namespace,
settings) -> list[RetrievedChunk]`.

Uses `PineconeVectorStore` (not the raw `IndexAsyncio.query` F2 uses directly) because the
refusal gate needs the top `dense_score` before deciding whether to call the LLM at all, and
`asimilarity_search_with_score` is the scored async surface LangChain provides for exactly that
— confirmed fully async when `index=` is an `_IndexAsyncio` instance (`_async_index_provided` in
`langchain_pinecone.vectorstores`), so this stays on the async surface end to end.
"""

import asyncio

from langchain_core.documents import Document

from app.core.contracts import RetrievedChunk
from app.indexing.vectorstore import get_index


def _build_embeddings(settings):
    from langchain_openai import OpenAIEmbeddings

    return OpenAIEmbeddings(
        model=settings.EMBED_MODEL, api_key=settings.OPENAI_API_KEY.get_secret_value()
    )


def _build_store(settings):
    from langchain_pinecone import PineconeVectorStore

    return PineconeVectorStore(
        index=get_index(settings), embedding=_build_embeddings(settings), text_key="text"
    )


async def compute_query_vector(query: str, settings) -> list[float]:
    """The query vector — F9's cache seam computes this ONCE per request and threads it back in as
    `query_vec` (design §2).

    Until F9, no query vector existed anywhere in the app: `asimilarity_search_with_score` embeds
    internally and discards the result, once per namespace per fan-out query (up to 3 × 2 = 6 embeds
    a request). The cache needs a vector *before* deciding whether to retrieve at all, so it embeds
    here and hands the vector down — which makes a miss CHEAPER than the old path (the normalized
    query's namespace fan-out stops re-embedding), not more expensive.

    Deliberately NOT named after LangChain's sync embed method: that name would make every call
    site read like the sync twin the `rag:` CI async guard greps for, and the guard is right to be
    literal about it — so the name moves instead. Uses the async `aembed_query` surface underneath.
    """
    return await _build_embeddings(settings).aembed_query(query)


def _none_if_sentinel(value: int | None) -> int | None:
    # F2's `_build_metadata` writes -1 for a null page_start/page_end (Pinecone metadata can't
    # store None) — undo that sentinel on the way back out.
    return None if value is None or value == -1 else value


def _to_retrieved_chunk(doc: Document, score: float) -> RetrievedChunk:
    md = doc.metadata
    return RetrievedChunk(
        chunk_id=doc.id,
        doc_id=md["doc_id"],
        title=md["title"],
        text=doc.page_content,
        section_heading=md.get("section_heading") or None,
        page_start=_none_if_sentinel(md.get("page_start")),
        page_end=_none_if_sentinel(md.get("page_end")),
        anchor=md.get("anchor") or None,
        dense_score=score,
    )


async def _retrieve_namespace(
    query: str, k: int, namespace: str, settings, query_vec: list[float] | None = None
) -> list[RetrievedChunk]:
    """`query_vec=None` takes the by-query surface (embeds internally) — byte-for-byte the
    pre-F9 path. When F9 supplies a vector, the by-vector surface skips the redundant embed."""
    store = _build_store(settings)
    if query_vec is not None:
        pairs = await store.asimilarity_search_by_vector_with_score(
            query_vec, k=k, namespace=namespace
        )
    else:
        pairs = await store.asimilarity_search_with_score(query, k=k, namespace=namespace)
    return [_to_retrieved_chunk(doc, score) for doc, score in pairs]


def _merge_top_k(*scored: list[RetrievedChunk], k: int) -> list[RetrievedChunk]:
    def _score(c: RetrievedChunk) -> float:
        return c.dense_score if c.dense_score is not None else float("-inf")

    merged = [chunk for group in scored for chunk in group]
    merged.sort(key=_score, reverse=True)
    return merged[:k]


async def dense_retrieve(
    query: str, k: int, namespace: str | None, settings, query_vec: list[float] | None = None
) -> list[RetrievedChunk]:
    """F3's dense-only retrieval — the `baseline` path, unchanged. `namespace=None` fans out over
    `settings.RETRIEVAL_NAMESPACES` (AC-4); a single namespace queries that namespace only.

    Fan-out uses plain `asyncio.gather` (no `return_exceptions=True`): if one namespace query
    raises, the whole call raises rather than silently returning only the other namespace's
    results — an incomplete top-k presented as complete would be worse than a loud failure. (F5's
    hybrid path catches that raise and degrades to BM25-only; dense_only propagates it as before.)

    F9: `query_vec` is reused across the namespace fan-out — the same vector queries both `pu` and
    `hec`, so a 2-namespace request drops from 2 embeds to 0.
    """
    if namespace is not None:
        return await _retrieve_namespace(query, k, namespace, settings, query_vec)

    results = await asyncio.gather(
        *(_retrieve_namespace(query, k, ns, settings, query_vec)
          for ns in settings.RETRIEVAL_NAMESPACES)
    )
    return _merge_top_k(*results, k=k)


def resolve_mode(settings) -> str:
    """Effective retrieval mode (AC-11/AC-13). `RETRIEVAL_MODE` is an eval-only explicit override
    that wins over `ENABLE_HYBRID`; otherwise hybrid iff `ENABLE_HYBRID`, else dense-only."""
    if settings.RETRIEVAL_MODE is not None:
        return settings.RETRIEVAL_MODE
    return "hybrid" if settings.ENABLE_HYBRID else "dense_only"


async def gather_candidate_pool(
    query: str, k: int, namespace: str | None, settings, query_vec: list[float] | None = None
) -> list[RetrievedChunk]:
    """The pre-rerank candidate pool for the effective retrieval mode (F5). Factored out of
    `retrieve` (F7) so both the single-query seam AND F7's multi-query fan-out
    (`rewrite.multi_query_retrieve`, which calls this once per rewritten query before a single
    shared rerank) share one pool-gathering code path.

    `dense_only` is byte-for-byte F3 (`baseline`); `hybrid` fuses BM25+dense (F5); `bm25_only` is
    an eval diagnostic (AC-13). When `ENABLE_RERANK` is on the pool is widened to
    `RERANK_CANDIDATE_K` (so rerank has more than `k` to re-order — hybrid already returns the
    ≤`HYBRID_FUSED_TOP_K` fused pool); with rerank off, `pool_k == k`. `hybrid` imported lazily to
    avoid the retriever↔hybrid import cycle.

    F9: `query_vec` (when supplied) rides down to the dense half of whichever mode runs. `bm25_only`
    ignores it — BM25 is lexical."""
    mode = resolve_mode(settings)
    # F6: widen the candidate pool when reranking so the cross-encoder can re-order more than `k`
    # (hybrid_retrieve already returns the fused pool ignoring `k`). With rerank off, pool_k == k so
    # dense_only/bm25_only stay byte-for-byte F5.
    pool_k = settings.RERANK_CANDIDATE_K if settings.ENABLE_RERANK else k

    if mode == "dense_only":
        return await dense_retrieve(query, pool_k, namespace, settings, query_vec)

    from app.rag import hybrid  # lazy: breaks the retriever↔hybrid import cycle

    if mode == "bm25_only":
        return await hybrid.sparse_only(query, pool_k, namespace, settings)
    return await hybrid.hybrid_retrieve(query, k, namespace, settings, query_vec)  # already ≤12


async def retrieve(
    query: str, k: int, namespace: str | None, settings, query_vec: list[float] | None = None
) -> list[RetrievedChunk]:
    """The F3→F5→F6 seam (signature unchanged bar F9's optional `query_vec`, AC-16/F6 AC-19):
    gather the candidate pool for the effective retrieval mode, then optionally rerank it (F6)
    before truncating to `k`.

    With rerank off this is byte-for-byte the F5 `pool[:k]` path (F6 AC-17); the count to generation
    stays `k` (=5). `rerank` imported lazily to avoid an import cycle (it imports this module). F7
    wraps this seam one layer out in `rewrite.retrieve` when `ENABLE_QUERY_REWRITE` is on."""
    pool = await gather_candidate_pool(query, k, namespace, settings, query_vec)

    if settings.ENABLE_RERANK:
        from app.rag import rerank  # lazy: breaks the retriever↔rerank import cycle

        return await rerank.rerank_chunks(query, pool, settings)
    return pool[:k]
