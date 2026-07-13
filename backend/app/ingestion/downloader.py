"""Polite, retrying, deduping downloader (T5/T6, AC-5..AC-10).

Rate limiting is a single `asyncio.Semaphore(1)` held for the duration of each request *plus*
a post-request sleep — the semaphore is only released after the sleep, so no two requests (even
retries of the same document) can start closer together than `1 / INGEST_RATE_LIMIT_PER_SEC`.
Retries (`tenacity`, async) only cover *transient* failures (AC-7): timeouts, connection errors,
and 5xx. A 4xx is a deterministic dead link (AC-9) and is not retried.
"""

from __future__ import annotations

import asyncio
import hashlib

import aiofiles
import httpx
import structlog
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.core.settings import Settings
from app.ingestion.schemas import DocStatus, DownloadOutcome, SourceRow

logger = structlog.get_logger(__name__)

_PDF_MAGIC = b"%PDF"
_ZIP_MAGIC = b"PK\x03\x04"
_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # legacy .doc/.ppt (AC-20) — not a mismatch


class _TransientHTTPError(Exception):
    """5xx response — retried (AC-7)."""


_RETRYABLE = (httpx.TimeoutException, httpx.TransportError, _TransientHTTPError)


def sniff_content_type(raw: bytes, declared: str) -> bool:
    """Pure-CPU magic-byte check (AC-8). Runs inline — cheap, no threading needed."""
    head = raw[:16]
    if declared == "pdf":
        return head.startswith(_PDF_MAGIC)
    if declared in {"docx", "pptx", "xlsx"}:
        # OOXML (current) or OLE2 (legacy .doc/.ppt, re-routed by loaders/legacy.py) are both OK.
        return head.startswith(_ZIP_MAGIC) or head.startswith(_OLE2_MAGIC)
    if declared == "html":
        sample = raw[:1024].lstrip().lower()
        return (
            sample.startswith(b"<!doctype html")
            or sample.startswith(b"<html")
            or b"<html" in sample
        )
    return True  # unknown declared type: routing rejects it separately, nothing to sniff here


async def _hash_file(path) -> str:
    async with aiofiles.open(path, "rb") as f:
        content = await f.read()
    return hashlib.sha256(content).hexdigest()


async def fetch(
    client: httpx.AsyncClient,
    row: SourceRow,
    rate_gate: asyncio.Semaphore,
    settings: Settings,
) -> DownloadOutcome:
    """AC-5..AC-10. Writes `data/raw/{doc_id}.{ext}` via aiofiles; skips the write on sha256
    dedupe match (AC-10)."""

    async def _get_once() -> httpx.Response:
        async with rate_gate:
            try:
                response = await client.get(
                    row.url,
                    timeout=settings.INGEST_DOWNLOAD_TIMEOUT_S,
                    follow_redirects=True,
                )
            finally:
                # held until the polite-crawl interval elapses, so the *next* acquirer (a fresh
                # request or this one's own retry) can't start any sooner.
                await asyncio.sleep(1.0 / settings.INGEST_RATE_LIMIT_PER_SEC)
        if response.status_code >= 500:
            raise _TransientHTTPError(f"{response.status_code} from {row.url}")
        return response

    try:
        response: httpx.Response | None = None
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(settings.INGEST_MAX_RETRIES),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
            retry=retry_if_exception_type(_RETRYABLE),
            reraise=True,
        ):
            with attempt:
                response = await _get_once()
    except _RETRYABLE as exc:
        logger.warning(
            "ingestion.downloader.dead_url", doc_id=row.doc_id, url=row.url, error=str(exc)
        )
        return DownloadOutcome(
            doc_id=row.doc_id, status=DocStatus.failed, note=f"dead URL after retries: {exc}"
        )

    assert response is not None  # AsyncRetrying always sets it or raises above

    if response.status_code >= 400:
        logger.warning(
            "ingestion.downloader.dead_url", doc_id=row.doc_id, url=row.url,
            status=response.status_code,
        )
        return DownloadOutcome(
            doc_id=row.doc_id, status=DocStatus.failed,
            note=f"dead URL: HTTP {response.status_code}",
        )

    raw = response.content
    if not sniff_content_type(raw, row.file_type):
        logger.warning(
            "ingestion.downloader.content_type_mismatch", doc_id=row.doc_id, declared=row.file_type,
        )
        return DownloadOutcome(
            doc_id=row.doc_id, status=DocStatus.failed,
            note=f"content-type mismatch: declared file_type={row.file_type!r}",
        )

    settings.RAW_DIR.mkdir(parents=True, exist_ok=True)
    raw_path = settings.RAW_DIR / f"{row.doc_id}.{row.file_type}"
    sha256 = hashlib.sha256(raw).hexdigest()

    if raw_path.exists() and await _hash_file(raw_path) == sha256:
        logger.info("ingestion.downloader.dedupe_skip", doc_id=row.doc_id)
        return DownloadOutcome(
            doc_id=row.doc_id, status=DocStatus.downloaded, path=raw_path, sha256=sha256,
            skipped_dedupe=True,
        )

    async with aiofiles.open(raw_path, "wb") as f:
        await f.write(raw)

    logger.info("ingestion.downloader.downloaded", doc_id=row.doc_id, bytes=len(raw))
    return DownloadOutcome(
        doc_id=row.doc_id, status=DocStatus.downloaded, path=raw_path, sha256=sha256
    )
