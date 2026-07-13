"""Source registry: parse + validate `sources.csv` (T3, AC-1/AC-3/AC-4), upsert into
`documents` (T4, AC-2).

Deviates from design.md §4's `load_sources(csv_path) -> list[SourceRow]` signature: a row that
fails validation can't always be represented as a `documents` row — `title`/`source_org`/
`file_type` are NOT NULL / CHECK-constrained, so genuinely incomplete or malformed data can't be
persisted as a `failed` document. AC-3's "mark it failed with a note" is therefore satisfied via
a report-only `IngestResult`, not a DB write; `load_sources` returns both the valid rows (T4
upserts these) and the rejected-row results (the run report, T16, folds these in).
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

import aiofiles
import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Document
from app.ingestion.schemas import (
    ALLOWED_FILE_TYPES,
    REQUIRED_SOURCE_COLUMNS,
    DocStatus,
    IngestResult,
    SourceRow,
)

logger = structlog.get_logger(__name__)

_ROW_FIELDS = REQUIRED_SOURCE_COLUMNS - {"notes"}  # every column but `notes` must be non-empty


class DuplicateDocIdError(Exception):
    """AC-4: two CSV rows share the same `doc_id` — abort the run before any download."""


async def load_sources(csv_path: Path) -> tuple[list[SourceRow], list[IngestResult]]:
    """Read + validate `sources.csv` (AC-1). Returns `(valid_rows, rejected_results)`.

    Duplicate `doc_id`s (AC-4) are checked first, across *all* rows regardless of per-row
    validity, and raise immediately — a structural CSV problem that must abort before any
    per-row validation or download proceeds.
    """
    async with aiofiles.open(csv_path, encoding="utf-8-sig", newline="") as f:
        raw = await f.read()

    reader = csv.DictReader(io.StringIO(raw))
    if reader.fieldnames is None:
        return [], []
    missing_header = REQUIRED_SOURCE_COLUMNS - set(reader.fieldnames)
    if missing_header:
        raise ValueError(f"sources.csv missing required column(s): {sorted(missing_header)}")

    raw_rows = list(reader)

    seen: dict[str, int] = {}
    duplicates: set[str] = set()
    for i, raw_row in enumerate(raw_rows, start=2):  # header is line 1
        doc_id = (raw_row.get("doc_id") or "").strip()
        if not doc_id:
            continue
        if doc_id in seen:
            duplicates.add(doc_id)
        else:
            seen[doc_id] = i
    if duplicates:
        raise DuplicateDocIdError(f"duplicate doc_id(s) in sources.csv: {sorted(duplicates)}")

    valid: list[SourceRow] = []
    rejected: list[IngestResult] = []

    for i, raw_row in enumerate(raw_rows, start=2):
        doc_id = (raw_row.get("doc_id") or "").strip()
        file_type = (raw_row.get("file_type") or "").strip()
        missing_fields = [c for c in _ROW_FIELDS if not (raw_row.get(c) or "").strip()]

        if missing_fields:
            note = f"row {i}: missing required field(s) {missing_fields}"
            rejected.append(
                IngestResult(
                    doc_id=doc_id or f"<row-{i}>", file_type=file_type,
                    status=DocStatus.failed, note=note,
                )
            )
            logger.warning("ingestion.registry.row_rejected", row=i, note=note)
            continue

        if file_type not in ALLOWED_FILE_TYPES:
            note = f"row {i}: file_type '{file_type}' not in {sorted(ALLOWED_FILE_TYPES)}"
            rejected.append(
                IngestResult(doc_id=doc_id, file_type=file_type, status=DocStatus.failed, note=note)
            )
            logger.warning("ingestion.registry.row_rejected", row=i, note=note)
            continue

        valid.append(
            SourceRow(
                doc_id=doc_id,
                title=raw_row["title"].strip(),
                source_org=raw_row["source_org"].strip(),
                url=raw_row["url"].strip(),
                file_type=file_type,
                version_label=raw_row["version_label"].strip(),
                notes=(raw_row.get("notes") or "").strip(),
            )
        )

    return valid, rejected


async def upsert_documents(session: AsyncSession, rows: list[SourceRow]) -> None:
    """AC-2: upsert each row into `documents` keyed on `doc_id`. New rows get
    `status=registered`; rows that already exist have their descriptive columns refreshed but
    **keep their current status** — a routine re-run must not silently reset a document that's
    already `downloaded`/`extracted` back to `registered`.
    """
    for row in rows:
        stmt = (
            pg_insert(Document)
            .values(
                doc_id=row.doc_id,
                title=row.title,
                source_org=row.source_org,
                url=row.url,
                file_type=row.file_type,
                version_label=row.version_label,
                is_scanned=False,
                status=DocStatus.registered,
            )
            .on_conflict_do_update(
                index_elements=[Document.doc_id],
                set_={
                    "title": row.title,
                    "source_org": row.source_org,
                    "url": row.url,
                    "file_type": row.file_type,
                    "version_label": row.version_label,
                },
            )
        )
        await session.execute(stmt)
    await session.flush()
