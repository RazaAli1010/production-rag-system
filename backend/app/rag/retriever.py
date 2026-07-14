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


def _build_store(settings):
    from langchain_openai import OpenAIEmbeddings
    from langchain_pinecone import PineconeVectorStore

    embeddings = OpenAIEmbeddings(
        model=settings.EMBED_MODEL, api_key=settings.OPENAI_API_KEY.get_secret_value()
    )
    return PineconeVectorStore(index=get_index(settings), embedding=embeddings, text_key="text")


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


async def _retrieve_namespace(query: str, k: int, namespace: str, settings) -> list[RetrievedChunk]:
    store = _build_store(settings)
    pairs = await store.asimilarity_search_with_score(query, k=k, namespace=namespace)
    return [_to_retrieved_chunk(doc, score) for doc, score in pairs]


def _merge_top_k(*scored: list[RetrievedChunk], k: int) -> list[RetrievedChunk]:
    def _score(c: RetrievedChunk) -> float:
        return c.dense_score if c.dense_score is not None else float("-inf")

    merged = [chunk for group in scored for chunk in group]
    merged.sort(key=_score, reverse=True)
    return merged[:k]


async def dense_retrieve(
    query: str, k: int, namespace: str | None, settings
) -> list[RetrievedChunk]:
    """F3's dense-only retrieval — the `baseline` path, unchanged. `namespace=None` fans out over
    `settings.RETRIEVAL_NAMESPACES` (AC-4); a single namespace queries that namespace only.

    Fan-out uses plain `asyncio.gather` (no `return_exceptions=True`): if one namespace query
    raises, the whole call raises rather than silently returning only the other namespace's
    results — an incomplete top-k presented as complete would be worse than a loud failure. (F5's
    hybrid path catches that raise and degrades to BM25-only; dense_only propagates it as before.)
    """
    if namespace is not None:
        return await _retrieve_namespace(query, k, namespace, settings)

    results = await asyncio.gather(
        *(_retrieve_namespace(query, k, ns, settings) for ns in settings.RETRIEVAL_NAMESPACES)
    )
    return _merge_top_k(*results, k=k)


def resolve_mode(settings) -> str:
    """Effective retrieval mode (AC-11/AC-13). `RETRIEVAL_MODE` is an eval-only explicit override
    that wins over `ENABLE_HYBRID`; otherwise hybrid iff `ENABLE_HYBRID`, else dense-only."""
    if settings.RETRIEVAL_MODE is not None:
        return settings.RETRIEVAL_MODE
    return "hybrid" if settings.ENABLE_HYBRID else "dense_only"


async def retrieve(
    query: str, k: int, namespace: str | None, settings
) -> list[RetrievedChunk]:
    """The F3→F5 seam (signature unchanged, AC-16): dispatches on the effective retrieval mode.

    `dense_only` is byte-for-byte F3 (`baseline`). `hybrid` fuses BM25+dense (F5) and returns the
    top `k` of the ≤`HYBRID_FUSED_TOP_K` fused pool, so the count handed to generation stays `k`
    (=5) until F6 inserts reranking (AC-9). `bm25_only` is an eval diagnostic (AC-13). `hybrid`
    imported lazily to avoid an import cycle (hybrid imports this module at top level)."""
    mode = resolve_mode(settings)
    if mode == "dense_only":
        return await dense_retrieve(query, k, namespace, settings)

    from app.rag import hybrid  # lazy: breaks the retriever↔hybrid import cycle

    if mode == "bm25_only":
        return await hybrid.sparse_only(query, k, namespace, settings)
    fused = await hybrid.hybrid_retrieve(query, k, namespace, settings)
    return fused[:k]
