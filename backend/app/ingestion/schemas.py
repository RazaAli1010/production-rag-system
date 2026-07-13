"""F1-local Pydantic models (design.md §1, §4).

`SourceRow`, `DownloadOutcome`, `ScanReport`, `IngestResult`, `RunReport` are F1-internal —
they never cross the F1/F2 seam (that seam is the `data/extracted/{doc_id}.jsonl` file plus the
`documents` row, per design.md §5). `DocStatus` re-exports the shared `DocumentStatus` enum
(owned by F12, `app.db.enums`) so ingestion code has one import surface for status values.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from app.db.enums import DocumentStatus as DocStatus

__all__ = [
    "DocStatus",
    "SourceRow",
    "DownloadOutcome",
    "ScanReport",
    "IngestResult",
    "RunReport",
]

ALLOWED_FILE_TYPES = frozenset({"pdf", "html", "docx", "pptx", "xlsx"})
REQUIRED_SOURCE_COLUMNS = frozenset(
    {"doc_id", "title", "source_org", "url", "file_type", "version_label", "notes"}
)


class SourceRow(BaseModel):
    """One validated row of `sources.csv` (AC-1)."""

    model_config = ConfigDict(str_strip_whitespace=True)

    doc_id: str
    title: str
    source_org: str
    url: str
    file_type: str
    version_label: str
    notes: str = ""


class DownloadOutcome(BaseModel):
    """Result of `downloader.fetch()` (AC-5, AC-8, AC-9, AC-10)."""

    doc_id: str
    status: DocStatus  # downloaded | failed
    path: Path | None = None  # local raw file; None if the fetch never wrote bytes
    sha256: str | None = None
    skipped_dedupe: bool = False  # AC-10: byte-identical to the cached copy, write skipped
    note: str | None = None


class ScanReport(BaseModel):
    """Result of `loaders.ocr.detect_scanned()` (AC-13)."""

    is_scanned: bool
    scanned_pages: list[int] = Field(default_factory=list)  # 1-indexed pages needing OCR
    total_pages: int
    scanned_ratio: float


class IngestResult(BaseModel):
    """Per-document outcome of the full pipeline, the unit `status.build_report()` aggregates."""

    doc_id: str
    file_type: str
    status: DocStatus
    is_scanned: bool = False
    page_count: int | None = None
    block_count: int = 0
    blocks_with_page_or_anchor: int = 0
    version_drift: bool = False
    html_links_only_pdf: bool = False  # AC-32 suggestion source
    dead_url: bool = False  # AC-32 report bucket
    note: str | None = None
    duration_ms: int | None = None


class RunReport(BaseModel):
    """Aggregate run summary (AC-32) — `status.build_report()` output."""

    generated_at: dt.datetime
    total: int
    counts_by_status: dict[str, int]
    scanned_count: int
    dead_url_doc_ids: list[str]
    html_link_only_suggestions: list[str]
    results: list[IngestResult]
