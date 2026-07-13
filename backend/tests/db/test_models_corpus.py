"""T-6: Document/Chunk — field mirroring + FK + cascade delete."""

import uuid
from datetime import UTC, datetime

import pytest

from app.db.enums import DocumentStatus
from app.db.models import Chunk, Document


def _doc_id():
    return f"test-doc-{uuid.uuid4().hex[:8]}-2026"


@pytest.mark.asyncio
async def test_document_and_chunk_insert(session):
    doc_id = _doc_id()
    doc = Document(
        doc_id=doc_id,
        title="Test Policy",
        source_org="PU",
        url="https://example.com/policy.pdf",
        file_type="pdf",
        downloaded_at=datetime.now(UTC),
        version_label="2026",
        is_scanned=False,
        page_count=10,
        sha256="a" * 64,
        status=DocumentStatus.registered,
    )
    session.add(doc)
    await session.flush()

    chunk = Chunk(
        chunk_id=f"{doc_id}:0",
        doc_id=doc_id,
        seq=0,
        text="Some chunk text.",
        section_heading="Section 1",
        page_start=1,
        page_end=1,
        anchor=None,
        token_count=4,
    )
    session.add(chunk)
    await session.flush()

    fetched_chunk = await session.get(Chunk, chunk.chunk_id)
    assert fetched_chunk.doc_id == doc_id
    assert fetched_chunk.seq == 0

    await session.delete(doc)
    await session.flush()
    # populate_existing bypasses the identity map so the assert reflects the DB-level cascade.
    assert await session.get(Chunk, chunk.chunk_id, populate_existing=True) is None
