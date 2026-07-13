import json

import aiofiles
import structlog
from langchain_core.documents import Document
from sqlalchemy import func, select

from app.db.enums import DocumentStatus
from app.db.models.corpus import Document as DocRow

logger = structlog.get_logger(__name__)


async def load_blocks(doc_id, settings):
    path = settings.EXTRACTED_DIR / f"{doc_id}.jsonl"
    if not path.exists():
        logger.warning("indexing.source.missing", doc_id=doc_id, path=str(path))
        return []
    async with aiofiles.open(path, encoding="utf-8") as f:
        raw = await f.read()
    docs = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        docs.append(Document(page_content=obj["page_content"], metadata=obj["metadata"]))
    return docs


async def indexed_targets(session, namespace, settings):
    stmt = select(DocRow).where(
        DocRow.status.in_([DocumentStatus.extracted, DocumentStatus.indexed])
    )
    if namespace != "all":
        stmt = stmt.where(func.lower(DocRow.source_org) == namespace)
    result = await session.execute(stmt)
    return list(result.scalars().all())
