"""T5: fetch + write + sha256 dedupe. T6: rate limit + retries + content-type sniff."""

import asyncio
import time

import httpx
import pytest

from app.core.settings import Settings
from app.db.enums import DocumentStatus
from app.ingestion.downloader import fetch, sniff_content_type
from app.ingestion.schemas import SourceRow


def _settings(**overrides) -> Settings:
    base = dict(
        DATABASE_URL="postgresql+asyncpg://u:p@localhost:5432/db",
        ADMIN_EMAIL="admin@example.com",
        ADMIN_PASSWORD="secret",
        INGEST_MAX_RETRIES=3,
        INGEST_RATE_LIMIT_PER_SEC=50.0,  # fast by default; individual tests override
        INGEST_DOWNLOAD_TIMEOUT_S=5.0,
    )
    base.update(overrides)
    return Settings(_env_file=None, **base)


def _row(**overrides) -> SourceRow:
    base = dict(
        doc_id="doc-x", title="Doc X", source_org="PU",
        url="https://example.com/doc-x.pdf", file_type="pdf", version_label="2021",
    )
    base.update(overrides)
    return SourceRow(**base)


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --- T5 -----------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_200_writes_file_and_returns_hash(tmp_path):
    settings = _settings()
    settings.RAW_DIR = tmp_path / "raw"

    body = b"%PDF-1.4 hello world"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    async with _client(handler) as client:
        outcome = await fetch(client, _row(), asyncio.Semaphore(1), settings)

    assert outcome.status == DocumentStatus.downloaded
    assert outcome.skipped_dedupe is False
    assert outcome.path.read_bytes() == body
    import hashlib
    assert outcome.sha256 == hashlib.sha256(body).hexdigest()


@pytest.mark.asyncio
async def test_fetch_identical_bytes_skips_write(tmp_path):
    settings = _settings()
    settings.RAW_DIR = tmp_path / "raw"
    body = b"%PDF-1.4 same bytes"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    async with _client(handler) as client:
        first = await fetch(client, _row(), asyncio.Semaphore(1), settings)
        mtime_before = first.path.stat().st_mtime_ns
        second = await fetch(client, _row(), asyncio.Semaphore(1), settings)

    assert second.skipped_dedupe is True
    assert second.sha256 == first.sha256
    assert second.path.stat().st_mtime_ns == mtime_before  # no rewrite happened


# --- T6 -----------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rate_limit_enforces_spacing(tmp_path):
    settings = _settings(INGEST_RATE_LIMIT_PER_SEC=5.0)  # 0.2s spacing, keeps test fast
    settings.RAW_DIR = tmp_path / "raw"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"%PDF-1.4 x")

    gate = asyncio.Semaphore(1)
    async with _client(handler) as client:
        start = time.monotonic()
        await fetch(client, _row(doc_id="doc-a"), gate, settings)
        await fetch(client, _row(doc_id="doc-b"), gate, settings)
        elapsed = time.monotonic() - start

    assert elapsed >= 0.2 - 0.02  # small tolerance for scheduler jitter


@pytest.mark.asyncio
async def test_5xx_retried_then_succeeds(tmp_path):
    settings = _settings(INGEST_RATE_LIMIT_PER_SEC=100.0)
    settings.RAW_DIR = tmp_path / "raw"
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, content=b"%PDF-1.4 recovered")

    async with _client(handler) as client:
        outcome = await fetch(client, _row(), asyncio.Semaphore(1), settings)

    assert calls["n"] == 3
    assert outcome.status == DocumentStatus.downloaded


@pytest.mark.asyncio
async def test_dead_url_4xx_not_retried_marks_failed(tmp_path):
    settings = _settings(INGEST_RATE_LIMIT_PER_SEC=100.0)
    settings.RAW_DIR = tmp_path / "raw"
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404)

    async with _client(handler) as client:
        outcome = await fetch(client, _row(), asyncio.Semaphore(1), settings)

    assert calls["n"] == 1  # no retry for a deterministic 4xx
    assert outcome.status == DocumentStatus.failed
    assert "404" in outcome.note


@pytest.mark.asyncio
async def test_content_type_mismatch_marks_failed(tmp_path):
    settings = _settings(INGEST_RATE_LIMIT_PER_SEC=100.0)
    settings.RAW_DIR = tmp_path / "raw"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html><body>not a pdf</body></html>")

    async with _client(handler) as client:
        outcome = await fetch(client, _row(file_type="pdf"), asyncio.Semaphore(1), settings)

    assert outcome.status == DocumentStatus.failed
    assert "mismatch" in outcome.note


# --- sniff_content_type unit checks --------------------------------------------------------

def test_sniff_content_type_pdf():
    assert sniff_content_type(b"%PDF-1.7 rest", "pdf") is True
    assert sniff_content_type(b"not a pdf", "pdf") is False


def test_sniff_content_type_office_accepts_ooxml_and_legacy_ole2():
    assert sniff_content_type(b"PK\x03\x04rest", "docx") is True
    assert sniff_content_type(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1rest", "pptx") is True
    assert sniff_content_type(b"nope", "xlsx") is False


def test_sniff_content_type_html():
    assert sniff_content_type(b"<!DOCTYPE html><html></html>", "html") is True
    assert sniff_content_type(b"%PDF-1.4", "html") is False
