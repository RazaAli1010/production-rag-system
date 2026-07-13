"""CLI entrypoint + orchestration (T17, AC-29/AC-30/AC-31): registry -> downloader -> version
drift -> (legacy re-route) -> routing/extraction -> cleaning -> serialize -> status; structured
per-doc timing/observability (T18, AC-31 — F1 makes no OpenAI calls, so no token/cost logging
applies here, per-doc timing + byte/page counts instead).

Per-document failures are isolated (AC-26) — one bad doc marks itself `failed` and the batch
continues. This includes version drift (AC-27's literal text: "abort **that document**... leave
the prior extracted artifacts intact") even though design.md §6's summary paragraph loosely
groups it with the one genuine whole-run abort, duplicate `doc_id` (AC-4, raised by
`load_sources` before any document is touched, propagating out of `main()`) — a single stale
upstream source shouldn't halt ingestion of every other unrelated document.

Without `--force`, a document already `extracted` is skipped entirely (no network call) — the
routine/incremental path. `--force` (AC-28) always re-downloads and re-extracts, and is the only
way a silent upstream content change (without a `version_label` bump) gets caught after the
first successful ingest.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import time

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import Settings
from app.core.settings import settings as default_settings
from app.db.engine import get_sessionmaker
from app.db.models import Document
from app.ingestion.cleaning import clean
from app.ingestion.downloader import fetch
from app.ingestion.loaders.legacy import (
    LegacyConversionError,
    convert_legacy,
    is_legacy_office_binary,
)
from app.ingestion.registry import load_sources, upsert_documents
from app.ingestion.routing import select_loader
from app.ingestion.schemas import DocStatus, IngestResult, SourceRow
from app.ingestion.serialize import write_jsonl
from app.ingestion.status import (
    VersionDriftError,
    build_report,
    check_version_drift,
    set_status,
    write_report,
)

logger = structlog.get_logger(__name__)


def _elapsed_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


async def ingest_one(
    session: AsyncSession,
    client: httpx.AsyncClient,
    row: SourceRow,
    rate_gate: asyncio.Semaphore,
    settings: Settings,
    *,
    force: bool = False,
) -> IngestResult:
    start = time.monotonic()
    log = logger.bind(doc_id=row.doc_id, file_type=row.file_type)

    if not force:
        existing = await session.get(Document, row.doc_id)
        if existing is not None and existing.status == DocStatus.extracted:
            log.info("ingestion.run.skip_already_extracted")
            return IngestResult(
                doc_id=row.doc_id, file_type=row.file_type, status=DocStatus.extracted,
                note="skipped: already extracted (use --force to re-check upstream)",
                duration_ms=_elapsed_ms(start),
            )

    # --- download (AC-5..AC-10) ---
    outcome = await fetch(client, row, rate_gate, settings)
    if outcome.status == DocStatus.failed:
        await set_status(session, row.doc_id, DocStatus.failed, note=outcome.note)
        log.warning("ingestion.run.download_failed", note=outcome.note)
        return IngestResult(
            doc_id=row.doc_id, file_type=row.file_type, status=DocStatus.failed,
            note=outcome.note, dead_url="dead URL" in (outcome.note or ""),
            duration_ms=_elapsed_ms(start),
        )

    # --- version drift (AC-27/AC-28) — a loud, batch-isolated abort for this one document ---
    try:
        await check_version_drift(session, row, outcome.sha256)
    except VersionDriftError as exc:
        # AC-26 still applies to a version-drift abort: the row is marked `failed` with a note
        # so the report/maintainer sees it, even though `sha256`/`downloaded_at` are left
        # untouched (AC-27's "leave the prior extracted artifacts intact" — the raw/JSONL files
        # from the last good run are never overwritten either, since we return before that step).
        await set_status(session, row.doc_id, DocStatus.failed, note=str(exc))
        log.error("ingestion.run.version_drift", error=str(exc))
        return IngestResult(
            doc_id=row.doc_id, file_type=row.file_type, status=DocStatus.failed,
            version_drift=True, note=str(exc), duration_ms=_elapsed_ms(start),
        )

    await set_status(
        session, row.doc_id, DocStatus.downloaded,
        sha256=outcome.sha256, downloaded_at=dt.datetime.now(dt.UTC),
    )

    # --- legacy .doc/.ppt re-route (AC-20) ---
    effective_path, effective_file_type = outcome.path, row.file_type
    if row.file_type in {"docx", "pptx"} and await is_legacy_office_binary(effective_path):
        try:
            effective_path, effective_file_type = await convert_legacy(effective_path, settings)
        except LegacyConversionError as exc:
            note = f"unsupported legacy format: {exc}"
            await set_status(session, row.doc_id, DocStatus.failed, note=note)
            log.error("ingestion.run.legacy_conversion_failed", error=str(exc))
            return IngestResult(
                doc_id=row.doc_id, file_type=row.file_type, status=DocStatus.failed,
                note=note, duration_ms=_elapsed_ms(start),
            )

    # --- routing + extraction (AC-11..AC-19); OCR/unstructured/parser crashes land here ---
    try:
        loader = select_loader(effective_file_type)
        blocks = await loader(effective_path, row.doc_id, settings)
    except Exception as exc:  # noqa: BLE001 — design.md's error table: isolate, don't crash the batch
        note = f"extraction failed: {exc}"
        await set_status(session, row.doc_id, DocStatus.failed, note=note)
        log.error("ingestion.run.extraction_failed", error=str(exc))
        return IngestResult(
            doc_id=row.doc_id, file_type=row.file_type, status=DocStatus.failed,
            note=note, duration_ms=_elapsed_ms(start),
        )

    is_scanned = any(b.metadata.get("is_scanned") for b in blocks)
    html_links_only_pdf = any(b.metadata.get("html_links_only_pdf") for b in blocks)

    # --- cleaning + serialize (AC-21..AC-25) ---
    cleaned = clean(blocks, settings)
    await write_jsonl(row.doc_id, cleaned, settings)

    page_numbers = {
        b.metadata.get("page_start") for b in cleaned if b.metadata.get("page_start") is not None
    }
    page_count = len(page_numbers) if page_numbers else None
    blocks_with_page_or_anchor = sum(
        1 for b in cleaned
        if b.metadata.get("page_start") is not None or b.metadata.get("anchor") is not None
    )

    await set_status(
        session, row.doc_id, DocStatus.extracted, page_count=page_count, is_scanned=is_scanned
    )

    duration_ms = _elapsed_ms(start)
    log.info(
        "ingestion.run.extracted", block_count=len(cleaned), page_count=page_count,
        is_scanned=is_scanned, duration_ms=duration_ms,
    )

    return IngestResult(
        doc_id=row.doc_id, file_type=effective_file_type, status=DocStatus.extracted,
        is_scanned=is_scanned, page_count=page_count, block_count=len(cleaned),
        blocks_with_page_or_anchor=blocks_with_page_or_anchor,
        html_links_only_pdf=html_links_only_pdf, duration_ms=duration_ms,
    )


def _select_rows(rows: list[SourceRow], args: argparse.Namespace) -> list[SourceRow]:
    if args.doc:
        return [r for r in rows if r.doc_id == args.doc]
    if args.file_type:
        return [r for r in rows if r.file_type == args.file_type]
    return rows  # --all, or no flag at all: everything


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="F1 — multi-format ingestion pipeline")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--all", action="store_true", help="ingest every registered source")
    group.add_argument("--doc", type=str, help="ingest a single doc_id")
    group.add_argument("--type", dest="file_type", type=str, help="ingest only this file_type")
    parser.add_argument(
        "--force", action="store_true", help="re-download/re-extract regardless of cached status"
    )
    return parser.parse_args(argv)


async def main(argv: list[str] | None = None, settings: Settings | None = None) -> None:
    args = _parse_args(argv)
    settings = settings or default_settings

    all_rows, rejected_results = await load_sources(settings.SOURCES_CSV)
    selected = _select_rows(all_rows, args)

    results: list[IngestResult] = list(rejected_results)
    rate_gate = asyncio.Semaphore(1)
    work_gate = asyncio.Semaphore(settings.INGEST_CONCURRENCY)

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as registry_session:
        # AC-2: the full CSV always registers, regardless of --doc/--type narrowing which
        # subset actually gets downloaded/extracted below.
        await upsert_documents(registry_session, all_rows)
        await registry_session.commit()

    async def _bounded(client: httpx.AsyncClient, row: SourceRow) -> IngestResult:
        # httpx.AsyncClient is safe to share across concurrent requests (it pools connections);
        # AsyncSession is NOT — each concurrent task gets its own.
        async with work_gate, sessionmaker() as task_session:
            try:
                result = await ingest_one(
                    task_session, client, row, rate_gate, settings, force=args.force
                )
                await task_session.commit()
                return result
            except Exception:
                await task_session.rollback()
                raise

    if selected:
        async with httpx.AsyncClient() as client:
            results.extend(await asyncio.gather(*(_bounded(client, row) for row in selected)))

    report = build_report(results)
    report_path = await write_report(report, settings)
    print(f"Ingestion run complete. Report: {report_path}")
    print(f"Totals by status: {report.counts_by_status}")


def _entrypoint() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    _entrypoint()
