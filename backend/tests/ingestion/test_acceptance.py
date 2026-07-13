"""T20 — F1 end-to-end acceptance / definition of done (requirements.md §4):

1. one command ingests all 5 file types + 1 scanned PDF, all reach `extracted`.
2. >= 95% of extracted blocks carry page or anchor metadata.
3. per-type fixtures committed (T19, `tests/fixtures/ingestion/`).
4. re-run is idempotent; mutated-fixture-same-label fails loudly (AC-27), old artifacts intact.
5. run report produced with all required sections.

The network layer is mocked (fixtures are served from disk instead of live PU/HEC URLs) — same
approach as `test_run.py` — so this suite needs no network access and never touches real sources.
"""

from __future__ import annotations

import hashlib
import json
import uuid

import fitz
import pytest

import app.ingestion.run as run_module
from app.db.enums import DocumentStatus
from app.db.models import Document
from app.ingestion.loaders import ocr as ocr_module
from app.ingestion.schemas import DocStatus, DownloadOutcome

HEADER = "doc_id,title,source_org,url,file_type,version_label,notes\n"

FIXTURE_MAP = {
    "pdf-digital": ("digital.pdf", "pdf"),
    "pdf-scanned": ("scanned.pdf", "pdf"),
    "html-sample": ("sample.html", "html"),
    "docx-sample": ("sample.docx", "docx"),
    "pptx-sample": ("sample.pptx", "pptx"),
    "xlsx-sample": ("sample.xlsx", "xlsx"),
}


def _uid() -> str:
    return uuid.uuid4().hex[:6]


@pytest.fixture
def doc_ids():
    suffix = _uid()
    return {key: f"acc-{key}-{suffix}" for key in FIXTURE_MAP}


def _write_sources_csv(csv_path, doc_ids: dict[str, str], version_label="2026") -> None:
    rows = "".join(
        f"{doc_ids[key]},{key} title,PU,https://example.com/{key},{file_type},{version_label},\n"
        for key, (_fixture, file_type) in FIXTURE_MAP.items()
    )
    csv_path.write_text(HEADER + rows, encoding="utf-8")


def _make_fake_fetch(
    fixtures_dir, doc_ids: dict[str, str], overrides: dict[str, bytes] | None = None
):
    overrides = overrides or {}
    calls: list[str] = []
    reverse = {doc_id: key for key, doc_id in doc_ids.items()}

    async def _fake_fetch(client, row, rate_gate, settings):
        calls.append(row.doc_id)
        key = reverse[row.doc_id]
        fixture_name, _file_type = FIXTURE_MAP[key]
        raw = overrides.get(row.doc_id) or (fixtures_dir / fixture_name).read_bytes()

        settings.RAW_DIR.mkdir(parents=True, exist_ok=True)
        path = settings.RAW_DIR / f"{row.doc_id}.{row.file_type}"
        path.write_bytes(raw)
        return DownloadOutcome(
            doc_id=row.doc_id, status=DocStatus.downloaded, path=path,
            sha256=hashlib.sha256(raw).hexdigest(),
        )

    return _fake_fetch, calls


def _fake_ocrmypdf_run(cmd, capture_output, text):
    """Stands in for the real `ocrmypdf` CLI (not installed locally/CI — Docker-image-only, per
    the plan's decision to mock at the subprocess boundary): writes a real replacement PDF with
    a text layer, simulating what OCR would recover."""
    # cmd: ["ocrmypdf", "-l", langs, "--pages", spec, str(src), str(out)]
    out_path = cmd[-1]
    doc = fitz.open()
    for i in range(2):
        # distinct per-page text — identical text on every page would (correctly) get stripped
        # as a repeated header/footer by cleaning.py's boilerplate detection (AC-21).
        text = f"OCR recovered text, page {i + 1}, scanned PU document."
        doc.new_page().insert_text((72, 72), text)
    doc.save(out_path)
    doc.close()

    class Result:
        returncode = 0
        stderr = ""

    return Result()


@pytest.mark.asyncio
async def test_f1_end_to_end_acceptance(
    tmp_ingest_dirs, monkeypatch, session, fixtures_dir, doc_ids
):
    from app.core.settings import settings as app_settings

    _write_sources_csv(app_settings.SOURCES_CSV, doc_ids)
    fake_fetch, calls = _make_fake_fetch(fixtures_dir, doc_ids)
    monkeypatch.setattr(run_module, "fetch", fake_fetch)
    monkeypatch.setattr(ocr_module.subprocess, "run", _fake_ocrmypdf_run)

    # --- 1. one command ingests all 5 types + 1 scanned PDF, all reach `extracted` -----------
    await run_module.main(["--all"])

    assert set(calls) == set(doc_ids.values())
    for doc_id in doc_ids.values():
        doc = await session.get(Document, doc_id, execution_options={"populate_existing": True})
        assert doc.status == DocumentStatus.extracted, f"{doc_id}: {doc.status} / {doc.note}"

    scanned_doc = await session.get(
        Document, doc_ids["pdf-scanned"], execution_options={"populate_existing": True}
    )
    assert scanned_doc.is_scanned is True

    # --- 2. >= 95% of extracted blocks carry page or anchor metadata -------------------------
    total_blocks = 0
    blocks_with_page_or_anchor = 0
    for doc_id in doc_ids.values():
        jsonl_path = app_settings.EXTRACTED_DIR / f"{doc_id}.jsonl"
        assert jsonl_path.exists()
        for line in jsonl_path.read_text(encoding="utf-8").splitlines():
            obj = json.loads(line)
            total_blocks += 1
            meta = obj["metadata"]
            if meta.get("page_start") is not None or meta.get("anchor") is not None:
                blocks_with_page_or_anchor += 1

    assert total_blocks > 0
    coverage = blocks_with_page_or_anchor / total_blocks
    assert coverage >= 0.95, f"only {coverage:.2%} of blocks carried page/anchor metadata"

    # --- 3. per-type fixtures committed (T19) -------------------------------------------------
    for fixture_name, _file_type in FIXTURE_MAP.values():
        assert (fixtures_dir / fixture_name).exists()

    # --- 4a. re-run is idempotent: already-extracted docs are skipped, not re-fetched --------
    calls.clear()
    await run_module.main(["--all"])
    assert calls == []
    for doc_id in doc_ids.values():
        doc = await session.get(Document, doc_id, execution_options={"populate_existing": True})
        assert doc.status == DocumentStatus.extracted

    # --- 4b. mutated fixture + unchanged version_label fails loudly, old artifacts intact ----
    digital_doc_id = doc_ids["pdf-digital"]
    original_jsonl = (app_settings.EXTRACTED_DIR / f"{digital_doc_id}.jsonl").read_text(
        encoding="utf-8"
    )
    mutated_bytes = b"%PDF-1.4\n% mutated content, not a real PDF structure change test\n"

    calls.clear()
    fake_fetch_mutated, _ = _make_fake_fetch(
        fixtures_dir, doc_ids, overrides={digital_doc_id: mutated_bytes}
    )
    monkeypatch.setattr(run_module, "fetch", fake_fetch_mutated)

    await run_module.main(["--doc", digital_doc_id, "--force"])

    drifted_doc = await session.get(
        Document, digital_doc_id, execution_options={"populate_existing": True}
    )
    assert drifted_doc.status == DocumentStatus.failed
    assert drifted_doc.note is not None and "version" in drifted_doc.note.lower()

    still_original = (app_settings.EXTRACTED_DIR / f"{digital_doc_id}.jsonl").read_text(
        encoding="utf-8"
    )
    assert still_original == original_jsonl  # old extracted artifacts left untouched

    # --- 5. run report produced with all required sections -----------------------------------
    report_files = list(app_settings.INGESTION_REPORT_DIR.glob("ingestion_report_*.md"))
    assert report_files, "no ingestion report was written"
    report_text = report_files[-1].read_text(encoding="utf-8")
    required_sections = (
        "Totals by status", "Scanned PDFs", "Dead URLs", "HTML pages that only link a PDF",
    )
    for section in required_sections:
        assert section in report_text
