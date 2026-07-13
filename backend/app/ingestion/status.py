"""`documents.status` transitions + version drift (T15, AC-26/AC-27/AC-28) and the run report
(T16, AC-32).
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import aiofiles
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import Settings
from app.db.models import Document
from app.ingestion.schemas import DocStatus, IngestResult, RunReport, SourceRow

_UNSET: Any = object()


class VersionDriftError(Exception):
    """AC-27: sha256 changed while `version_label` didn't — abort this document loudly and
    leave its prior `extracted` artifacts intact. `--force` (AC-28) still hits this guard;
    only bumping `version_label` clears it."""


async def check_version_drift(session: AsyncSession, row: SourceRow, new_sha256: str) -> None:
    existing = await session.get(Document, row.doc_id)
    if existing is None or existing.sha256 is None:
        return  # never downloaded before — nothing to drift from
    if existing.sha256 != new_sha256 and existing.version_label == row.version_label:
        raise VersionDriftError(
            f"{row.doc_id}: content changed (sha256 {existing.sha256[:12]}... -> "
            f"{new_sha256[:12]}...) but version_label ({row.version_label!r}) was not bumped"
        )


async def set_status(
    session: AsyncSession,
    doc_id: str,
    status: DocStatus,
    *,
    note: Any = _UNSET,
    sha256: Any = _UNSET,
    downloaded_at: Any = _UNSET,
    page_count: Any = _UNSET,
    is_scanned: Any = _UNSET,
) -> None:
    """AC-26: transitions `documents.status`, writing whichever of
    `page_count`/`is_scanned`/`sha256`/`downloaded_at`/`note` are supplied (unsupplied kwargs
    are left untouched — e.g. the `extracted` transition doesn't repeat the `sha256` already
    recorded at the `downloaded` transition)."""
    doc = await session.get(Document, doc_id)
    if doc is None:
        raise ValueError(f"set_status: no documents row for doc_id={doc_id!r}")

    doc.status = status
    if note is not _UNSET:
        doc.note = note
    if sha256 is not _UNSET:
        doc.sha256 = sha256
    if downloaded_at is not _UNSET:
        doc.downloaded_at = downloaded_at
    if page_count is not _UNSET:
        doc.page_count = page_count
    if is_scanned is not _UNSET:
        doc.is_scanned = is_scanned

    await session.flush()


def build_report(results: list[IngestResult]) -> RunReport:
    """AC-32: totals by status, scanned count, dead URLs, HTML-links-only-a-PDF suggestions."""
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status.value] = counts.get(r.status.value, 0) + 1

    return RunReport(
        generated_at=dt.datetime.now(dt.UTC),
        total=len(results),
        counts_by_status=counts,
        scanned_count=sum(1 for r in results if r.is_scanned),
        dead_url_doc_ids=[r.doc_id for r in results if r.dead_url],
        html_link_only_suggestions=[r.doc_id for r in results if r.html_links_only_pdf],
        results=results,
    )


def render_markdown(report: RunReport) -> str:
    lines = [
        f"# Ingestion run report — {report.generated_at.isoformat()}",
        "",
        f"Total documents processed: **{report.total}**",
        "",
        "## Totals by status",
        "",
    ]
    for status, count in sorted(report.counts_by_status.items()):
        lines.append(f"- `{status}`: {count}")

    lines += ["", "## Scanned PDFs", "", f"Scanned-PDF count: **{report.scanned_count}**"]

    lines += ["", "## Dead URLs", ""]
    lines += [f"- {doc_id}" for doc_id in report.dead_url_doc_ids] or ["- (none)"]

    lines += ["", "## HTML pages that only link a PDF (suggest registering the PDF directly)", ""]
    lines += [f"- {doc_id}" for doc_id in report.html_link_only_suggestions] or ["- (none)"]

    lines += ["", "## Per-document results", "", "| doc_id | status | note |", "|---|---|---|"]
    for r in report.results:
        note = (r.note or "").replace("|", "\\|")
        lines.append(f"| {r.doc_id} | {r.status.value} | {note} |")

    return "\n".join(lines) + "\n"


async def write_report(report: RunReport, settings: Settings) -> Path:
    """AC-32: write `docs/ingestion_report_{ts}.md` (stdout summary is the CLI's job, T17)."""
    settings.INGESTION_REPORT_DIR.mkdir(parents=True, exist_ok=True)
    ts = report.generated_at.strftime("%Y%m%dT%H%M%SZ")
    out_path = settings.INGESTION_REPORT_DIR / f"ingestion_report_{ts}.md"
    async with aiofiles.open(out_path, "w", encoding="utf-8") as f:
        await f.write(render_markdown(report))
    return out_path
