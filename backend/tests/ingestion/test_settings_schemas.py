"""T1: Settings() loads all F1 keys with defaults + env overrides; schema models round-trip."""

from pathlib import Path

from app.core.settings import Settings
from app.ingestion.schemas import (
    DocStatus,
    DownloadOutcome,
    IngestResult,
    RunReport,
    ScanReport,
    SourceRow,
)


def _base_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost:5432/db")
    monkeypatch.setenv("ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("ADMIN_PASSWORD", "secret")


def test_ingestion_settings_defaults(monkeypatch):
    _base_env(monkeypatch)

    s = Settings(_env_file=None)

    assert s.DATA_DIR == Path("app/data")
    assert s.RAW_DIR == Path("app/data/raw")
    assert s.EXTRACTED_DIR == Path("app/data/extracted")
    assert s.SOURCES_CSV == Path("app/data/sources.csv")
    assert s.INGEST_CONCURRENCY == 4
    assert s.INGEST_RATE_LIMIT_PER_SEC == 1.0
    assert s.INGEST_MAX_RETRIES == 3
    assert s.INGEST_DOWNLOAD_TIMEOUT_S == 60.0
    assert s.OCR_LANGUAGES == "eng+urd"
    assert s.OCR_MIN_PAGE_TEXT_CHARS == 50
    assert s.OCR_SCANNED_PAGE_THRESHOLD == 0.30
    assert s.CLEAN_HEADER_FOOTER_PAGE_RATIO == 0.60
    assert s.CLEAN_MIN_BLOCK_CHARS == 20
    assert s.LIBREOFFICE_BIN == "libreoffice"
    assert s.INGESTION_REPORT_DIR == Path("../docs")


def test_ingestion_settings_env_overrides(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("INGEST_CONCURRENCY", "8")
    monkeypatch.setenv("OCR_SCANNED_PAGE_THRESHOLD", "0.5")
    monkeypatch.setenv("SOURCES_CSV", "custom/sources.csv")

    s = Settings(_env_file=None)

    assert s.INGEST_CONCURRENCY == 8
    assert s.OCR_SCANNED_PAGE_THRESHOLD == 0.5
    assert s.SOURCES_CSV == Path("custom/sources.csv")


def test_source_row_roundtrip():
    row = SourceRow(
        doc_id="hec-plagiarism-policy-2021",
        title="HEC Plagiarism Policy",
        source_org="HEC",
        url="https://hec.gov.pk/plagiarism.pdf",
        file_type="pdf",
        version_label="2021",
        notes="  padded  ",
    )
    dumped = row.model_dump()
    restored = SourceRow.model_validate(dumped)
    assert restored == row
    assert restored.notes == "padded"  # whitespace-stripped


def test_download_outcome_roundtrip(tmp_path):
    outcome = DownloadOutcome(
        doc_id="doc-1",
        status=DocStatus.downloaded,
        path=tmp_path / "doc-1.pdf",
        sha256="a" * 64,
        skipped_dedupe=False,
    )
    restored = DownloadOutcome.model_validate(outcome.model_dump())
    assert restored == outcome


def test_scan_report_roundtrip():
    report = ScanReport(is_scanned=True, scanned_pages=[1, 3, 5], total_pages=10, scanned_ratio=0.3)
    restored = ScanReport.model_validate(report.model_dump())
    assert restored == report


def test_ingest_result_and_run_report_roundtrip():
    result = IngestResult(
        doc_id="doc-1",
        file_type="pdf",
        status=DocStatus.extracted,
        page_count=12,
        block_count=40,
        blocks_with_page_or_anchor=40,
    )
    report = RunReport(
        generated_at="2026-07-13T00:00:00Z",
        total=1,
        counts_by_status={"extracted": 1},
        scanned_count=0,
        dead_url_doc_ids=[],
        html_link_only_suggestions=[],
        results=[result],
    )
    restored = RunReport.model_validate(report.model_dump())
    assert restored.results[0].doc_id == "doc-1"
    assert restored.counts_by_status == {"extracted": 1}
