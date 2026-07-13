from sqlalchemy import delete

from app.db.enums import DocumentStatus
from app.db.models.corpus import Chunk as ChunkRow
from app.db.models.corpus import Document as DocRow


async def replace_chunks(session, doc_id, chunks):
    await session.execute(delete(ChunkRow).where(ChunkRow.doc_id == doc_id))
    session.add_all([ChunkRow(**c.model_dump()) for c in chunks])
    await session.flush()


async def mark_indexed(session, doc_id):
    row = await session.get(DocRow, doc_id)
    row.status = DocumentStatus.indexed
    await session.flush()
