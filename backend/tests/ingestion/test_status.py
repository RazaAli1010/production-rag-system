"""T15: status transitions + version-drift abort. T16: run report sections/counts (AC-26/
AC-27/AC-28/AC-32)."""

import uuid

import pytest

from app.core.settings import Settings
from app.db.enums import DocumentStatus
from app.db.models import Document
from app.ingestion.registry import upsert_documents
from app.ingestion.schemas import DocStatus, IngestResult, SourceRow
from app.ingestion.status import (
    VersionDriftError,
    build_report,
    check_version_drift,
    render_markdown,
    set_status,
    write_report,
)


def _doc_id():
    return f"test-status-{uuid.uuid4().hex[:8]}"


def _settings(report_dir) -> Settings:
    s = Settings(
        _env_file=None,
        DATABASE_URL="postgresql+asyncpg://u:p@localhost:5432/db",
        ADMIN_EMAIL="admin@example.com",
        ADMIN_PASSWORD="secret",
        OPENAI_API_KEY="sk-test",
        PINECONE_API_KEY="pc-test",
        PINECONE_INDEX="campus-rag",
    )
    s.INGESTION_REPORT_DIR = report_dir
    return s


async def _seed_doc(session, doc_id, version_label="2021") -> SourceRow:
    row = SourceRow(
        doc_id=doc_id, title="Doc", source_org="PU", url="https://example.com/x.pdf",
        file_type="pdf", version_label=version_label,
    )
    await upsert_documents(session, [row])
    return row


# --- T15: transitions ------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_happy_path_transitions_recorded(session):
    doc_id = _doc_id()
    await _seed_doc(session, doc_id)

    import datetime as dt
    now = dt.datetime.now(dt.UTC)
    await set_status(session, doc_id, DocStatus.downloaded, sha256="a" * 64, downloaded_at=now)

    doc = await session.get(Document, doc_id, populate_existing=True)
    assert doc.status == DocumentStatus.downloaded
    assert doc.sha256 == "a" * 64
    assert doc.downloaded_at is not None

    await set_status(session, doc_id, DocStatus.extracted, page_count=12, is_scanned=False)

    doc = await session.get(Document, doc_id, populate_existing=True)
    assert doc.status == DocumentStatus.extracted
    assert doc.page_count == 12
    assert doc.is_scanned is False
    assert doc.sha256 == "a" * 64  # untouched by the second call, not wiped


@pytest.mark.asyncio
async def test_set_status_unknown_doc_id_raises(session):
    with pytest.raises(ValueError, match="no documents row"):
        await set_status(session, "does-not-exist", DocStatus.failed, note="x")


@pytest.mark.asyncio
async def test_set_status_note_only_failure(session):
    doc_id = _doc_id()
    await _seed_doc(session, doc_id)

    await set_status(session, doc_id, DocStatus.failed, note="dead URL: HTTP 404")

    doc = await session.get(Document, doc_id, populate_existing=True)
    assert doc.status == DocumentStatus.failed
    assert doc.note == "dead URL: HTTP 404"


# --- T15: version drift -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_first_download_never_drifts(session):
    doc_id = _doc_id()
    row = await _seed_doc(session, doc_id)
    await check_version_drift(session, row, new_sha256="a" * 64)  # no prior sha256 — no-op


@pytest.mark.asyncio
async def test_mutated_bytes_same_label_aborts(session):
    doc_id = _doc_id()
    row = await _seed_doc(session, doc_id)
    await set_status(session, doc_id, DocStatus.downloaded, sha256="a" * 64)

    with pytest.raises(VersionDriftError, match=doc_id):
        await check_version_drift(session, row, new_sha256="b" * 64)

    # prior status/sha256 left intact — check_version_drift never mutates
    doc = await session.get(Document, doc_id, populate_existing=True)
    assert doc.sha256 == "a" * 64


@pytest.mark.asyncio
async def test_bumped_label_proceeds(session):
    doc_id = _doc_id()
    row = await _seed_doc(session, doc_id, version_label="2021")
    await set_status(session, doc_id, DocStatus.downloaded, sha256="a" * 64)

    bumped_row = row.model_copy(update={"version_label": "2022"})
    await check_version_drift(session, bumped_row, new_sha256="b" * 64)  # no raise


@pytest.mark.asyncio
async def test_identical_bytes_never_drifts(session):
    doc_id = _doc_id()
    row = await _seed_doc(session, doc_id)
    await set_status(session, doc_id, DocStatus.downloaded, sha256="a" * 64)

    await check_version_drift(session, row, new_sha256="a" * 64)  # same hash — no raise


# --- T16: run report ---------------------------------------------------------------------------

def test_build_report_totals_and_buckets():
    results = [
        IngestResult(doc_id="a", file_type="pdf", status=DocStatus.extracted, is_scanned=True),
        IngestResult(doc_id="b", file_type="html", status=DocStatus.extracted,
                      html_links_only_pdf=True),
        IngestResult(doc_id="c", file_type="pdf", status=DocStatus.failed, dead_url=True,
                      note="dead URL: HTTP 404"),
    ]
    report = build_report(results)

    assert report.total == 3
    assert report.counts_by_status == {"extracted": 2, "failed": 1}
    assert report.scanned_count == 1
    assert report.dead_url_doc_ids == ["c"]
    assert report.html_link_only_suggestions == ["b"]


def test_render_markdown_contains_required_sections():
    results = [
        IngestResult(doc_id="a", file_type="pdf", status=DocStatus.extracted, is_scanned=True),
        IngestResult(doc_id="c", file_type="pdf", status=DocStatus.failed, dead_url=True),
    ]
    md = render_markdown(build_report(results))

    assert "Totals by status" in md
    assert "Scanned PDFs" in md
    assert "Dead URLs" in md
    assert "HTML pages that only link a PDF" in md
    assert "- c" in md  # dead url doc id listed


@pytest.mark.asyncio
async def test_write_report_writes_markdown_file(tmp_path):
    results = [IngestResult(doc_id="a", file_type="pdf", status=DocStatus.extracted)]
    report = build_report(results)

    out_path = await write_report(report, _settings(tmp_path / "docs"))

    assert out_path.exists()
    assert out_path.name.startswith("ingestion_report_")
    assert "Totals by status" in out_path.read_text(encoding="utf-8")
