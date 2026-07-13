import json

from sqlalchemy import select

from app.core.settings import settings as global_settings
from app.db.engine import get_sessionmaker
from app.db.enums import DocumentStatus
from app.db.models.corpus import Chunk as ChunkRow
from app.db.models.corpus import Document as DocRow
from app.indexing import run as run_module


class FakeIndex:
    def __init__(self):
        self.vectors = {}

    async def upsert(self, vectors, namespace):
        self.vectors.setdefault(namespace, {}).update({v["id"]: v for v in vectors})

    async def delete(self, delete_all, namespace):
        self.vectors.pop(namespace, None)


class FakeEmbeddings:
    async def aembed_documents(self, texts):
        return [[float(len(t))] * 3 for t in texts]


async def _seed(doc_id, org, status=DocumentStatus.extracted):
    async with get_sessionmaker()() as session:
        session.add(DocRow(doc_id=doc_id, title=f"T-{doc_id}", source_org=org, url="http://x",
                           file_type="pdf", version_label="v1", is_scanned=False, status=status))
        await session.commit()


def _block(text, page=1):
    return {"page_content": text, "metadata": {"doc_id": "x", "page_start": page,
            "page_end": page, "anchor": None, "section_heading": None}}


def _write_jsonl(doc_id, blocks):
    path = global_settings.EXTRACTED_DIR / f"{doc_id}.jsonl"
    path.write_text("\n".join(json.dumps(b) for b in blocks) + "\n", encoding="utf-8")


async def test_run_indexes_and_reconciles(tmp_index_dirs):
    await _seed("pu-doc-2021", "PU")
    _write_jsonl("pu-doc-2021", [_block("clause one body text"), _block("clause two body")])
    idx = FakeIndex()
    report = await run_module.main(
        argv=["--strategy", "fixed", "--namespace", "pu"], index=idx, embeddings=FakeEmbeddings(),
    )
    assert report.results[0].status == "indexed"
    async with get_sessionmaker()() as check:
        rows = (await check.execute(select(ChunkRow))).scalars().all()
        doc = await check.get(DocRow, "pu-doc-2021")
    assert len(idx.vectors["pu"]) == len(rows)
    assert doc.status == DocumentStatus.indexed
    assert global_settings.BM25_PATH.exists()
    assert global_settings.INDEX_MANIFEST_PATH.exists()


async def test_run_skips_missing_jsonl(tmp_index_dirs):
    await _seed("pu-nofile-2021", "PU")
    report = await run_module.main(argv=["--strategy", "fixed", "--namespace", "pu"],
                                   index=FakeIndex(), embeddings=FakeEmbeddings())
    assert "pu-nofile-2021" in report.skipped
