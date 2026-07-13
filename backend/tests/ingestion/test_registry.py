"""T3: load_sources parse+validate. T4: upsert_documents preserves status on re-upsert."""

import uuid

import pytest
from sqlalchemy import select

from app.db.enums import DocumentStatus
from app.db.models import Document
from app.ingestion.registry import DuplicateDocIdError, load_sources, upsert_documents

HEADER = "doc_id,title,source_org,url,file_type,version_label,notes\n"


def _write_csv(path, body: str) -> None:
    path.write_text(HEADER + body, encoding="utf-8")


def _doc_id():
    return f"test-reg-{uuid.uuid4().hex[:8]}"


@pytest.mark.asyncio
async def test_valid_csv_returns_source_rows(tmp_path):
    csv_path = tmp_path / "sources.csv"
    _write_csv(
        csv_path,
        "doc-a,Title A,PU,https://example.com/a.pdf,pdf,2021,\n"
        "doc-b,Title B,HEC,https://example.com/b.html,html,2022,some note\n",
    )

    rows, rejected = await load_sources(csv_path)

    assert rejected == []
    assert [r.doc_id for r in rows] == ["doc-a", "doc-b"]
    assert rows[1].notes == "some note"
    assert rows[0].file_type == "pdf"


@pytest.mark.asyncio
async def test_missing_column_row_rejected_with_note(tmp_path):
    csv_path = tmp_path / "sources.csv"
    _write_csv(
        csv_path,
        "doc-a,Title A,PU,https://example.com/a.pdf,pdf,2021,\n"
        "doc-b,,HEC,https://example.com/b.html,html,2022,\n",  # missing title
    )

    rows, rejected = await load_sources(csv_path)

    assert [r.doc_id for r in rows] == ["doc-a"]
    assert len(rejected) == 1
    assert rejected[0].doc_id == "doc-b"
    assert rejected[0].status == DocumentStatus.failed
    assert "missing required field" in rejected[0].note


@pytest.mark.asyncio
async def test_invalid_file_type_row_rejected_with_note(tmp_path):
    csv_path = tmp_path / "sources.csv"
    _write_csv(csv_path, "doc-a,Title A,PU,https://example.com/a.mp4,mp4,2021,\n")

    rows, rejected = await load_sources(csv_path)

    assert rows == []
    assert len(rejected) == 1
    assert "file_type" in rejected[0].note


@pytest.mark.asyncio
async def test_duplicate_doc_id_aborts_run(tmp_path):
    csv_path = tmp_path / "sources.csv"
    _write_csv(
        csv_path,
        "dup,Title A,PU,https://example.com/a.pdf,pdf,2021,\n"
        "dup,Title B,HEC,https://example.com/b.pdf,pdf,2021,\n",
    )

    with pytest.raises(DuplicateDocIdError, match="dup"):
        await load_sources(csv_path)


@pytest.mark.asyncio
async def test_missing_required_header_raises(tmp_path):
    csv_path = tmp_path / "sources.csv"
    csv_path.write_text("doc_id,title\nx,y\n", encoding="utf-8")

    with pytest.raises(ValueError, match="missing required column"):
        await load_sources(csv_path)


@pytest.mark.asyncio
async def test_upsert_new_rows_then_reupsert_preserves_status(session):
    from app.ingestion.schemas import SourceRow

    doc_id = _doc_id()
    row = SourceRow(
        doc_id=doc_id, title="Original Title", source_org="PU",
        url="https://example.com/x.pdf", file_type="pdf", version_label="2021",
    )
    await upsert_documents(session, [row])

    fetched = await session.scalar(select(Document).where(Document.doc_id == doc_id))
    assert fetched.title == "Original Title"
    assert fetched.status == DocumentStatus.registered

    # simulate the doc having progressed past registered
    fetched.status = DocumentStatus.extracted
    await session.flush()

    changed_row = row.model_copy(update={"title": "Updated Title"})
    await upsert_documents(session, [changed_row])

    count = await session.scalar(
        select(Document.doc_id).where(Document.doc_id == doc_id)
    )
    assert count == doc_id  # single row per doc_id (no duplicate insert)

    refetched = await session.scalar(
        select(Document).where(Document.doc_id == doc_id).execution_options(populate_existing=True)
    )
    assert refetched.title == "Updated Title"
    assert refetched.status == DocumentStatus.extracted  # status preserved, not reset
