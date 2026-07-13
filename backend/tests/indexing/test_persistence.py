from sqlalchemy import select

from app.core.contracts import Chunk
from app.db.enums import DocumentStatus
from app.db.models.corpus import Chunk as ChunkRow
from app.db.models.corpus import Document as DocRow
from app.indexing.persistence import mark_indexed, replace_chunks


async def _seed_doc(session, doc_id="d"):
    session.add(DocRow(doc_id=doc_id, title="T", source_org="PU", url="http://x",
                       file_type="pdf", version_label="v1", is_scanned=False,
                       status=DocumentStatus.extracted))
    await session.flush()


def _chunks(doc_id, n):
    return [Chunk(chunk_id=f"{doc_id}:{i}", doc_id=doc_id, seq=i, text=f"t{i}", token_count=2)
            for i in range(n)]


async def test_replace_chunks_inserts(session):
    await _seed_doc(session)
    await replace_chunks(session, "d", _chunks("d", 3))
    rows = (await session.execute(select(ChunkRow).where(ChunkRow.doc_id == "d"))).scalars().all()
    assert len(rows) == 3
    assert {r.chunk_id for r in rows} == {"d:0", "d:1", "d:2"}


async def test_replace_chunks_no_orphans_on_reindex(session):
    await _seed_doc(session)
    await replace_chunks(session, "d", _chunks("d", 5))
    await replace_chunks(session, "d", _chunks("d", 2))
    rows = (await session.execute(select(ChunkRow).where(ChunkRow.doc_id == "d"))).scalars().all()
    assert len(rows) == 2


async def test_mark_indexed(session):
    await _seed_doc(session)
    await mark_indexed(session, "d")
    row = await session.get(DocRow, "d")
    assert row.status == DocumentStatus.indexed
