"""T18: log capture shows one structured event per doc with timing + counts; no OpenAI/token
logging is attempted (F1 makes no OpenAI calls) (AC-31)."""

import hashlib
import uuid

import fitz
import pytest
import structlog.testing

import app.ingestion.run as run_module
from app.ingestion.schemas import DocStatus, DownloadOutcome


def _uid():
    return uuid.uuid4().hex[:8]


def _make_pdf_bytes() -> bytes:
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "Structured logging test document body text.")
    data = doc.tobytes()
    doc.close()
    return data


@pytest.mark.asyncio
async def test_ingest_one_emits_structured_extracted_event_with_timing_and_counts(
    tmp_ingest_dirs, monkeypatch, session
):
    doc_id = f"doc-log-{_uid()}"
    from app.core.settings import settings as app_settings
    from app.ingestion.registry import upsert_documents
    from app.ingestion.schemas import SourceRow

    row = SourceRow(
        doc_id=doc_id, title="Log Test", source_org="PU",
        url="https://example.com/log.pdf", file_type="pdf", version_label="2021",
    )
    await upsert_documents(session, [row])
    await session.commit()

    raw = _make_pdf_bytes()

    async def fake_fetch(client, row, rate_gate, settings):
        app_settings.RAW_DIR.mkdir(parents=True, exist_ok=True)
        path = app_settings.RAW_DIR / f"{row.doc_id}.{row.file_type}"
        path.write_bytes(raw)
        return DownloadOutcome(
            doc_id=row.doc_id, status=DocStatus.downloaded, path=path,
            sha256=hashlib.sha256(raw).hexdigest(),
        )

    monkeypatch.setattr(run_module, "fetch", fake_fetch)

    import asyncio

    import httpx

    with structlog.testing.capture_logs() as captured:
        async with httpx.AsyncClient() as client:
            await run_module.ingest_one(session, client, row, asyncio.Semaphore(1), app_settings)

    extracted_events = [e for e in captured if e["event"] == "ingestion.run.extracted"]
    assert len(extracted_events) == 1
    event = extracted_events[0]

    assert event["doc_id"] == doc_id
    assert "duration_ms" in event and isinstance(event["duration_ms"], int)
    assert "block_count" in event
    assert "page_count" in event

    # AC-31: F1 makes no OpenAI calls — no token/cost fields on any captured event.
    for e in captured:
        assert "tokens_in" not in e
        assert "tokens_out" not in e
        assert "est_cost_usd" not in e
