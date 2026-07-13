import asyncio

import structlog

from app.core.contracts import Chunk
from app.core.settings import Settings
from app.indexing.embedder import embed_chunks


def _settings():
    return Settings(_env_file=None, DATABASE_URL="postgresql+asyncpg://u:p@localhost:5432/db",
                    ADMIN_EMAIL="a@b.c", ADMIN_PASSWORD="x", OPENAI_API_KEY="k",
                    PINECONE_API_KEY="k", PINECONE_INDEX="i")


class Emb:
    async def aembed_documents(self, texts):
        return [[1.0] for _ in texts]


async def test_every_embed_batch_logs_cost():
    chunks = [Chunk(chunk_id=f"d:{i}", doc_id="d", seq=i, text="t", token_count=2)
              for i in range(120)]
    with structlog.testing.capture_logs() as logs:
        await embed_chunks(chunks, Emb(), asyncio.Semaphore(2), _settings())
    batch_events = [e for e in logs if e["event"] == "indexing.embed.batch"]
    assert len(batch_events) == 2
    assert all("est_cost_usd" in e and "tokens_in" in e for e in batch_events)
