"""`GET /api/documents` — corpus listing for the UI (F11, AC-6).

Read-only projection of the `documents` table (F12/F1-owned). Public: the corpus is public
regulation text, and the UI shows "what's covered" before login.
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Document
from app.db.session import get_session

router = APIRouter(prefix="/api", tags=["documents"])


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    doc_id: str
    title: str
    source_org: str
    version_label: str
    file_type: str
    url: str
    status: str


@router.get("/documents", response_model=list[DocumentOut],
            summary="List the indexed corpus",
            description="Every registered document with its org, version, type, URL and status.")
async def list_documents(session: AsyncSession = Depends(get_session)) -> list[DocumentOut]:
    rows = (await session.execute(select(Document).order_by(Document.doc_id))).scalars().all()
    return [DocumentOut.model_validate(r) for r in rows]
