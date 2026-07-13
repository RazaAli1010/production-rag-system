import asyncio
import json

from app.core.contracts import Chunk
from app.core.settings import Settings
from app.indexing.vectorstore import _build_metadata, upsert


def _settings(**o):
    return Settings(_env_file=None, DATABASE_URL="postgresql+asyncpg://u:p@localhost:5432/db",
                    ADMIN_EMAIL="a@b.c", ADMIN_PASSWORD="x", OPENAI_API_KEY="k",
                    PINECONE_API_KEY="k", PINECONE_INDEX="i", **o)


class FakeIndex:
    def __init__(self, fail_first=0):
        self.upserts = []
        self.calls = 0
        self.fail_first = fail_first

    async def upsert(self, vectors, namespace):
        self.calls += 1
        if self.calls <= self.fail_first:
            err = Exception("rate")
            err.status_code = 429
            raise err
        self.upserts.append((namespace, vectors))


def _chunk(i, text="body"):
    return Chunk(chunk_id=f"d:{i}", doc_id="d", seq=i, text=text, section_heading="H",
                 page_start=1, page_end=1, anchor=None, token_count=2)


async def test_upsert_sets_id_namespace_metadata():
    s = _settings()
    idx = FakeIndex()
    chunks = [_chunk(0), _chunk(1)]
    n = await upsert(idx, chunks, [[1.0], [2.0]], "pu", "Doc Title", asyncio.Semaphore(2), s)
    assert n == 2
    ns, vectors = idx.upserts[0]
    assert ns == "pu"
    all_ids = {v["id"] for _, group in idx.upserts for v in group}
    assert all_ids == {"d:0", "d:1"}
    assert vectors[0]["metadata"]["title"] == "Doc Title"
    assert vectors[0]["metadata"]["doc_id"] == "d"


def test_metadata_truncates_over_40kb():
    s = _settings(PINECONE_METADATA_MAX_BYTES=1000)
    md = _build_metadata(_chunk(0, text="x" * 5000), "T", s)
    assert len(json.dumps(md, ensure_ascii=False).encode("utf-8")) <= 1000


def test_metadata_has_no_null_values():
    s = _settings()
    md = _build_metadata(_chunk(0), "T", s)
    assert all(v is not None for v in md.values())


async def test_upsert_backs_off_on_rate_limit():
    s = _settings(EMBED_MAX_RETRIES=3)
    idx = FakeIndex(fail_first=1)
    n = await upsert(idx, [_chunk(0)], [[1.0]], "pu", "T", asyncio.Semaphore(1), s)
    assert n == 1 and idx.calls == 2
