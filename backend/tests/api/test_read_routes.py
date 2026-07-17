"""T6 — /api/documents + /api/history (AC-5/6). History is empty until F13 writes request_logs;
that empty result is the correct behaviour, asserted here."""

import uuid

import pytest_asyncio

from app.db.models.corpus import Document


@pytest_asyncio.fixture
async def seeded_doc(sessionmaker_):
    """Insert one clearly-test document and remove it afterwards — the cleanup fixture deliberately
    never truncates `documents` (corpus-protection), so this test owns its row's lifecycle."""
    doc_id = f"test-doc-{uuid.uuid4().hex[:8]}"
    async with sessionmaker_() as db:
        db.add(Document(
            doc_id=doc_id, title="Test Reg", source_org="PU", url="http://x", file_type="pdf",
            version_label="2026", is_scanned=False, page_count=1, status="indexed",
        ))
        await db.commit()
    yield doc_id
    async with sessionmaker_() as db:
        d = await db.get(Document, doc_id)
        if d is not None:
            await db.delete(d)
            await db.commit()


async def test_documents_lists_seeded(client, seeded_doc):
    r = await client.get("/api/documents")
    assert r.status_code == 200
    ids = {d["doc_id"] for d in r.json()}
    assert seeded_doc in ids
    row = next(d for d in r.json() if d["doc_id"] == seeded_doc)
    assert row["source_org"] == "PU" and row["status"] == "indexed"


async def test_history_requires_auth(client):
    r = await client.get("/api/history")
    assert r.status_code == 401


async def test_history_empty_pre_f13(client, student):
    r = await client.get("/api/history", headers=student["headers"])
    assert r.status_code == 200
    assert r.json() == []  # correct: F13 has not written request_logs yet
