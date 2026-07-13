"""T17: `--doc <id>` ingests one; `--type pdf` filters; `--force` re-runs; one failing doc does
not abort the batch (AC-29/AC-30)."""

import hashlib
import uuid

import fitz
import pytest

import app.ingestion.run as run_module
from app.db.enums import DocumentStatus
from app.db.models import Document
from app.ingestion.schemas import DocStatus, DownloadOutcome

HEADER = "doc_id,title,source_org,url,file_type,version_label,notes\n"


def _write_csv(path, body: str) -> None:
    path.write_text(HEADER + body, encoding="utf-8")


def _uid():
    return uuid.uuid4().hex[:8]


def _make_pdf_bytes() -> bytes:
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "Some real digital text content for a test document.")
    data = doc.tobytes()
    doc.close()
    return data


def _make_fake_fetch(bytes_by_doc_id: dict[str, bytes], fail_doc_ids: set[str] | None = None):
    fail_doc_ids = fail_doc_ids or set()
    calls: list[str] = []

    async def _fake_fetch(client, row, rate_gate, settings):
        calls.append(row.doc_id)
        if row.doc_id in fail_doc_ids:
            return DownloadOutcome(
                doc_id=row.doc_id, status=DocStatus.failed, note="dead URL: HTTP 404"
            )
        raw = bytes_by_doc_id[row.doc_id]
        settings.RAW_DIR.mkdir(parents=True, exist_ok=True)
        path = settings.RAW_DIR / f"{row.doc_id}.{row.file_type}"
        path.write_bytes(raw)
        return DownloadOutcome(
            doc_id=row.doc_id, status=DocStatus.downloaded, path=path,
            sha256=hashlib.sha256(raw).hexdigest(),
        )

    return _fake_fetch, calls


@pytest.mark.asyncio
async def test_doc_flag_ingests_only_that_document(tmp_ingest_dirs, monkeypatch, session):
    a, b = f"doc-a-{_uid()}", f"doc-b-{_uid()}"
    from app.core.settings import settings as app_settings

    _write_csv(
        app_settings.SOURCES_CSV,
        f"{a},A,PU,https://example.com/a.pdf,pdf,2021,\n"
        f"{b},B,PU,https://example.com/b.pdf,pdf,2021,\n",
    )
    fake_fetch, calls = _make_fake_fetch({a: _make_pdf_bytes(), b: _make_pdf_bytes()})
    monkeypatch.setattr(run_module, "fetch", fake_fetch)

    await run_module.main(["--doc", a])

    assert calls == [a]
    doc_a = await session.get(Document, a)
    doc_b = await session.get(Document, b)
    assert doc_a.status == DocumentStatus.extracted
    assert doc_b.status == DocumentStatus.registered  # untouched — never selected


@pytest.mark.asyncio
async def test_type_flag_filters_by_file_type(tmp_ingest_dirs, monkeypatch, session):
    pdf_id, html_id = f"doc-pdf-{_uid()}", f"doc-html-{_uid()}"
    from app.core.settings import settings as app_settings

    _write_csv(
        app_settings.SOURCES_CSV,
        f"{pdf_id},P,PU,https://example.com/p.pdf,pdf,2021,\n"
        f"{html_id},H,PU,https://example.com/h.html,html,2021,\n",
    )
    fake_fetch, calls = _make_fake_fetch({pdf_id: _make_pdf_bytes()})
    monkeypatch.setattr(run_module, "fetch", fake_fetch)

    await run_module.main(["--type", "pdf"])

    assert calls == [pdf_id]


@pytest.mark.asyncio
async def test_force_forces_reingest_otherwise_skipped(tmp_ingest_dirs, monkeypatch, session):
    doc_id = f"doc-force-{_uid()}"
    from app.core.settings import settings as app_settings

    _write_csv(app_settings.SOURCES_CSV, f"{doc_id},T,PU,https://example.com/t.pdf,pdf,2021,\n")
    fake_fetch, calls = _make_fake_fetch({doc_id: _make_pdf_bytes()})
    monkeypatch.setattr(run_module, "fetch", fake_fetch)

    await run_module.main(["--doc", doc_id])
    assert calls == [doc_id]

    await run_module.main(["--doc", doc_id])  # no --force: already extracted, should skip
    assert calls == [doc_id]  # fetch not called again

    await run_module.main(["--doc", doc_id, "--force"])
    assert calls == [doc_id, doc_id]  # forced: fetch called again


@pytest.mark.asyncio
async def test_one_failing_doc_does_not_abort_batch(tmp_ingest_dirs, monkeypatch, session):
    ok_id, bad_id = f"doc-ok-{_uid()}", f"doc-bad-{_uid()}"
    from app.core.settings import settings as app_settings

    _write_csv(
        app_settings.SOURCES_CSV,
        f"{ok_id},OK,PU,https://example.com/ok.pdf,pdf,2021,\n"
        f"{bad_id},BAD,PU,https://example.com/bad.pdf,pdf,2021,\n",
    )
    fake_fetch, calls = _make_fake_fetch(
        {ok_id: _make_pdf_bytes(), bad_id: b""}, fail_doc_ids={bad_id}
    )
    monkeypatch.setattr(run_module, "fetch", fake_fetch)

    await run_module.main(["--all"])

    assert set(calls) == {ok_id, bad_id}
    doc_ok = await session.get(Document, ok_id, execution_options={"populate_existing": True})
    doc_bad = await session.get(Document, bad_id, execution_options={"populate_existing": True})
    assert doc_ok.status == DocumentStatus.extracted
    assert doc_bad.status == DocumentStatus.failed
    assert doc_bad.note == "dead URL: HTTP 404"
