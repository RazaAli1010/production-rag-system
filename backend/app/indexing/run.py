import argparse
import asyncio
from collections import namedtuple
from datetime import UTC, datetime

import structlog
from sqlalchemy import func, select

from app.core.settings import settings as default_settings
from app.db.engine import get_sessionmaker
from app.db.models.corpus import Chunk as ChunkRow
from app.db.models.corpus import Document as DocRow
from app.indexing.bm25 import build_and_pickle
from app.indexing.chunkers.base import select_chunker
from app.indexing.cost import estimate_cost
from app.indexing.embedder import embed_chunks
from app.indexing.manifest import guard_strategy, write_manifest
from app.indexing.persistence import mark_indexed, replace_chunks
from app.indexing.schemas import IndexResult, Manifest, RunReport
from app.indexing.source import indexed_targets, load_blocks
from app.indexing.vectorstore import get_index, upsert, wipe_namespace

logger = structlog.get_logger(__name__)
Gates = namedtuple("Gates", "embed upsert")


async def index_one(session, index, embeddings, row, chunker, gates, settings):
    docs = await load_blocks(row.doc_id, settings)
    namespace = row.source_org.lower()
    if not docs:
        result = IndexResult(doc_id=row.doc_id, namespace=namespace, chunk_count=0,
                             tokens_in=0, cost_usd=0.0, status="skipped")
        return result, []

    chunks = chunker.split(docs, row.doc_id)
    vectors = await embed_chunks(chunks, embeddings, gates.embed, settings)
    await upsert(index, chunks, vectors, namespace, row.title, gates.upsert, settings)
    await replace_chunks(session, row.doc_id, chunks)
    await mark_indexed(session, row.doc_id)

    tokens_in = sum(c.token_count for c in chunks)
    cost = estimate_cost(settings.EMBED_MODEL, tokens_in)
    logger.info("indexing.run.doc", doc_id=row.doc_id, namespace=namespace,
                chunk_count=len(chunks), tokens_in=tokens_in, est_cost_usd=cost)
    result = IndexResult(doc_id=row.doc_id, namespace=namespace, chunk_count=len(chunks),
                         tokens_in=tokens_in, cost_usd=cost, status="indexed")
    return result, chunks


def _parse_args(argv):
    p = argparse.ArgumentParser(prog="app.indexing.run")
    p.add_argument("--strategy", choices=["fixed", "structure"])
    p.add_argument("--namespace", choices=["pu", "hec", "all"], default="all")
    p.add_argument("--wipe", action="store_true")
    p.add_argument("--bm25-only", action="store_true",
                   help="rebuild data/bm25.pkl from the persisted chunks; no embedding/upsert")
    return p.parse_args(argv)


async def rebuild_bm25(sessionmaker, settings):
    """BM25 spans the whole persisted corpus, not just one run's docs — a single-namespace or
    resumed run must not silently shrink the sparse index to the subset it touched. Also the
    deploy path: bm25.pkl is a build artifact, rebuildable from Postgres without re-embedding.
    """
    async with sessionmaker() as session:
        corpus = (await session.execute(
            select(ChunkRow.chunk_id, ChunkRow.text).order_by(ChunkRow.chunk_id)
        )).all()
    await build_and_pickle([c.text for c in corpus], [c.chunk_id for c in corpus], settings)
    return len(corpus)


async def main(argv=None, settings=None, index=None, embeddings=None):
    args = _parse_args(argv)
    settings = settings or default_settings
    strategy = args.strategy or settings.INDEXING_STRATEGY

    if args.bm25_only:
        count = await rebuild_bm25(get_sessionmaker(), settings)
        print(f"bm25 rebuilt from {count} persisted chunks -> {settings.BM25_PATH}")
        return None

    await guard_strategy(strategy, args.wipe, settings)

    if index is None:
        index = get_index(settings)
    if embeddings is None:
        from langchain_openai import OpenAIEmbeddings
        embeddings = OpenAIEmbeddings(model=settings.EMBED_MODEL,
                                      api_key=settings.OPENAI_API_KEY.get_secret_value())

    chunker = select_chunker(strategy, settings)
    gates = Gates(asyncio.Semaphore(settings.EMBED_CONCURRENCY),
                  asyncio.Semaphore(settings.PINECONE_UPSERT_CONCURRENCY))
    sessionmaker = get_sessionmaker()

    async with sessionmaker() as target_session:
        rows = await indexed_targets(target_session, args.namespace, settings)

    namespaces = ["pu", "hec"] if args.namespace == "all" else [args.namespace]
    if args.wipe:
        async with sessionmaker() as wipe_session:
            for ns in namespaces:
                await wipe_namespace(index, wipe_session, ns, settings)
            await wipe_session.commit()

    results, skipped = [], []
    for row in rows:
        async with sessionmaker() as doc_session:
            try:
                result, chunks = await index_one(
                    doc_session, index, embeddings, row, chunker, gates, settings
                )
                await doc_session.commit()
            except Exception:
                await doc_session.rollback()
                logger.error("indexing.run.doc_failed", doc_id=row.doc_id, exc_info=True)
                result = IndexResult(doc_id=row.doc_id, namespace=row.source_org.lower(),
                                     chunk_count=0, tokens_in=0, cost_usd=0.0, status="failed")
                chunks = []
            if result.status == "skipped":
                skipped.append(row.doc_id)
            results.append(result)

    await rebuild_bm25(sessionmaker, settings)

    manifest_ns = {}
    async with sessionmaker() as recon_session:
        # The manifest always describes the whole index; only the namespaces this run touched
        # can be reconciled against freshly upserted vector counts.
        for ns in ("pu", "hec"):
            db_count = await recon_session.scalar(
                select(func.count()).select_from(ChunkRow).join(DocRow)
                .where(func.lower(DocRow.source_org) == ns))
            if ns in namespaces:
                vec_count = sum(
                    r.chunk_count for r in results if r.namespace == ns and r.status == "indexed"
                )
                if db_count != vec_count:
                    raise SystemExit(
                        f"reconcile mismatch ns={ns}: vectors={vec_count} chunks={db_count}"
                    )
            manifest_ns[ns] = {"vectors": db_count, "chunks": db_count}

    total_tokens = sum(r.tokens_in for r in results)
    total_cost = sum(r.cost_usd for r in results)
    manifest = Manifest(strategy=strategy, embed_model=settings.EMBED_MODEL,
                        namespaces=manifest_ns, total_tokens=total_tokens,
                        est_cost_usd=total_cost, created_at=datetime.now(UTC).isoformat())
    manifest_path = await write_manifest(manifest, settings)
    logger.info("indexing.run.summary", strategy=strategy, total_tokens=total_tokens,
                est_cost_usd=total_cost, docs=len(results), skipped=len(skipped))
    print(f"indexed {len(results)} docs · {total_tokens} tokens · ${total_cost:.6f}")
    return RunReport(strategy=strategy, results=results, skipped=skipped,
                     total_tokens=total_tokens, total_cost_usd=total_cost,
                     manifest_path=str(manifest_path))


def _entrypoint():
    asyncio.run(main())


if __name__ == "__main__":
    _entrypoint()
