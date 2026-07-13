import asyncio
import json

import structlog
import tenacity
from sqlalchemy import delete, func, select

from app.db.models.corpus import Chunk as ChunkRow
from app.db.models.corpus import Document as DocRow
from app.indexing.embedder import _batch, _is_rate_limit

logger = structlog.get_logger(__name__)


def _build_metadata(chunk, title, settings):
    md = {
        "doc_id": chunk.doc_id,
        "title": title,
        "section_heading": chunk.section_heading or "",
        "page_start": chunk.page_start if chunk.page_start is not None else -1,
        "page_end": chunk.page_end if chunk.page_end is not None else -1,
        "anchor": chunk.anchor or "",
        "token_count": chunk.token_count,
        "text": chunk.text,
    }
    encoded = json.dumps(md, ensure_ascii=False).encode("utf-8")
    if len(encoded) > settings.PINECONE_METADATA_MAX_BYTES:
        overflow = len(encoded) - settings.PINECONE_METADATA_MAX_BYTES
        raw = chunk.text.encode("utf-8")
        md["text"] = raw[: max(0, len(raw) - overflow - 64)].decode("utf-8", "ignore")
        logger.warning("indexing.upsert.metadata_truncated", chunk_id=chunk.chunk_id)
    return md


async def upsert(index, chunks, vectors, namespace, title, gate, settings):
    pairs = list(zip(chunks, vectors, strict=True))
    batches = list(_batch(pairs, settings.EMBED_BATCH_SIZE))
    counts = await asyncio.gather(
        *(_upsert_batch(index, b, namespace, title, gate, settings) for b in batches)
    )
    return sum(counts)


async def _upsert_batch(index, batch, namespace, title, gate, settings):
    vectors = [
        {"id": c.chunk_id, "values": v, "metadata": _build_metadata(c, title, settings)}
        for c, v in batch
    ]
    async for attempt in tenacity.AsyncRetrying(
        retry=tenacity.retry_if_exception(_is_rate_limit),
        wait=tenacity.wait_exponential(min=1, max=30),
        stop=tenacity.stop_after_attempt(settings.EMBED_MAX_RETRIES),
        reraise=True,
    ):
        with attempt:
            async with gate:
                await index.upsert(vectors=vectors, namespace=namespace)
    return len(vectors)


async def wipe_namespace(index, session, namespace, settings):
    await index.delete(delete_all=True, namespace=namespace)
    doc_ids = select(DocRow.doc_id).where(func.lower(DocRow.source_org) == namespace)
    await session.execute(delete(ChunkRow).where(ChunkRow.doc_id.in_(doc_ids)))
    logger.warning("indexing.wipe", namespace=namespace)


def get_index(settings):
    from pinecone import Pinecone

    pc = Pinecone(api_key=settings.PINECONE_API_KEY.get_secret_value())
    host = pc.describe_index(settings.PINECONE_INDEX).host
    return pc.IndexAsyncio(host=host)
