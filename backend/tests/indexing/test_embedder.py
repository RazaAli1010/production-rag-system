import asyncio

import structlog

from app.core.contracts import Chunk
from app.core.settings import Settings
from app.indexing.embedder import _batch, embed_chunks


def _settings(**o):
    return Settings(_env_file=None, DATABASE_URL="postgresql+asyncpg://u:p@localhost:5432/db",
                    ADMIN_EMAIL="a@b.c", ADMIN_PASSWORD="x", OPENAI_API_KEY="k",
                    PINECONE_API_KEY="k", PINECONE_INDEX="i", **o)


def _chunks(n):
    return [Chunk(chunk_id=f"d:{i}", doc_id="d", seq=i, text=f"t{i}", token_count=3)
            for i in range(n)]


def test_batch_helper():
    assert list(_batch(list(range(5)), 2)) == [[0, 1], [2, 3], [4]]


async def test_embeds_all_in_batches_order_preserved():
    s = _settings(EMBED_BATCH_SIZE=100, EMBED_CONCURRENCY=4)

    class Emb:
        async def aembed_documents(self, texts):
            return [[float(len(t))] for t in texts]

    chunks = _chunks(250)
    vecs = await embed_chunks(chunks, Emb(), asyncio.Semaphore(4), s)
    assert len(vecs) == 250


async def test_retries_on_rate_limit():
    s = _settings(EMBED_BATCH_SIZE=100, EMBED_MAX_RETRIES=3)

    class Emb:
        def __init__(self):
            self.calls = 0

        async def aembed_documents(self, texts):
            self.calls += 1
            if self.calls == 1:
                err = Exception("rate limited")
                err.status_code = 429
                raise err
            return [[1.0] for _ in texts]

    emb = Emb()
    vecs = await embed_chunks(_chunks(5), emb, asyncio.Semaphore(1), s)
    assert len(vecs) == 5 and emb.calls == 2


async def test_logs_cost_per_batch():
    s = _settings(EMBED_BATCH_SIZE=100)

    class Emb:
        async def aembed_documents(self, texts):
            return [[1.0] for _ in texts]

    with structlog.testing.capture_logs() as logs:
        await embed_chunks(_chunks(3), Emb(), asyncio.Semaphore(1), s)
    assert any("est_cost_usd" in e for e in logs)
