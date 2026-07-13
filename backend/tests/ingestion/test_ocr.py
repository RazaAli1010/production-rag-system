"""T9: detect_scanned per-page/doc-level rule (AC-13); ocr_pdf subprocess (mocked); mixed PDFs
OCR only scanned pages and preserve digital pages' original text (AC-14)."""

import fitz
import pytest

from app.core.settings import Settings
from app.ingestion.loaders import ocr as ocr_module
from app.ingestion.loaders.ocr import OCRFailedError, detect_scanned, ocr_pdf
from app.ingestion.loaders.pdf import load_pdf


def _settings(**overrides) -> Settings:
    base = dict(
        DATABASE_URL="postgresql+asyncpg://u:p@localhost:5432/db",
        ADMIN_EMAIL="admin@example.com",
        ADMIN_PASSWORD="secret",
    )
    base.update(overrides)
    return Settings(_env_file=None, **base)


def _insert_image(page, text: str | None = None) -> None:
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 100, 100))
    pix.set_rect(pix.irect, (200, 200, 200))
    page.insert_image(fitz.Rect(50, 50, 150, 150), pixmap=pix)
    if text:
        page.insert_text((72, 200), text)


def _make_scanned_pdf(path):
    doc = fitz.open()
    _insert_image(doc.new_page())  # image, no text => scanned page
    doc.save(str(path))
    doc.close()


def _make_digital_pdf(path):
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "Plenty of real digital text on this page, no scan.")
    doc.save(str(path))
    doc.close()


def _make_mixed_pdf(path):
    doc = fitz.open()
    _insert_image(doc.new_page())  # page 1: scanned
    doc.new_page().insert_text((72, 72), "Digital page original text stays untouched.")  # page 2
    doc.save(str(path))
    doc.close()


# --- detect_scanned -------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_detect_scanned_flags_image_only_pdf(tmp_path):
    path = tmp_path / "scanned.pdf"
    _make_scanned_pdf(path)

    report = await detect_scanned(path, _settings())

    assert report.is_scanned is True
    assert report.scanned_pages == [1]
    assert report.total_pages == 1
    assert report.scanned_ratio == 1.0


@pytest.mark.asyncio
async def test_detect_scanned_false_for_digital_pdf(tmp_path):
    path = tmp_path / "digital.pdf"
    _make_digital_pdf(path)

    report = await detect_scanned(path, _settings())

    assert report.is_scanned is False
    assert report.scanned_pages == []


@pytest.mark.asyncio
async def test_detect_scanned_mixed_below_threshold_not_doc_level_scanned(tmp_path):
    # 1 scanned page out of 4 = 25%, below the default 30% threshold => doc-level not scanned,
    # even though that one page IS individually flagged.
    doc = fitz.open()
    _insert_image(doc.new_page())
    for _ in range(3):
        doc.new_page().insert_text((72, 72), "Digital text page.")
    path = tmp_path / "mostly_digital.pdf"
    doc.save(str(path))
    doc.close()

    report = await detect_scanned(path, _settings())

    assert report.scanned_pages == [1]
    assert report.is_scanned is False  # 0.25 <= 0.30 threshold


# --- ocr_pdf (subprocess mocked) -------------------------------------------------------------

@pytest.mark.asyncio
async def test_ocr_pdf_success_returns_output_path(tmp_path, monkeypatch):
    path = tmp_path / "scanned.pdf"
    _make_scanned_pdf(path)

    def fake_run(cmd, capture_output, text):
        # simulate ocrmypdf: write a real replacement PDF with an added text layer on page 1
        out_path = tmp_path / "scanned.ocr.pdf"
        doc = fitz.open()
        p = doc.new_page()
        _insert_image(p, text="OCR recovered text.")
        doc.save(str(out_path))
        doc.close()

        class Result:
            returncode = 0
            stderr = ""

        return Result()

    monkeypatch.setattr(ocr_module.subprocess, "run", fake_run)

    output_path = await ocr_pdf(path, [1], _settings())

    assert output_path.exists()
    assert output_path.name == "scanned.ocr.pdf"


@pytest.mark.asyncio
async def test_ocr_pdf_failure_raises(tmp_path, monkeypatch):
    path = tmp_path / "scanned.pdf"
    _make_scanned_pdf(path)

    def fake_run(cmd, capture_output, text):
        class Result:
            returncode = 1
            stderr = "tesseract not found"

        return Result()

    monkeypatch.setattr(ocr_module.subprocess, "run", fake_run)

    with pytest.raises(OCRFailedError, match="tesseract not found"):
        await ocr_pdf(path, [1], _settings())


# --- load_pdf end-to-end orchestration (scan-detect -> OCR -> reload) ------------------------

@pytest.mark.asyncio
async def test_load_pdf_scanned_document_ocrs_and_reloads(tmp_path, monkeypatch):
    path = tmp_path / "scanned.pdf"
    _make_scanned_pdf(path)

    def fake_run(cmd, capture_output, text):
        out_path = tmp_path / "scanned.ocr.pdf"
        doc = fitz.open()
        _insert_image(doc.new_page(), text="OCR recovered text.")
        doc.save(str(out_path))
        doc.close()

        class Result:
            returncode = 0
            stderr = ""

        return Result()

    monkeypatch.setattr(ocr_module.subprocess, "run", fake_run)

    blocks = await load_pdf(path, "doc-scanned", _settings())

    assert blocks, "post-OCR reload should yield non-empty text"
    assert all(b.metadata["is_scanned"] for b in blocks)
    assert any("OCR recovered text." in b.page_content for b in blocks)


@pytest.mark.asyncio
async def test_load_pdf_mixed_document_preserves_digital_page_text(tmp_path, monkeypatch):
    path = tmp_path / "mixed.pdf"
    _make_mixed_pdf(path)

    def fake_run(cmd, capture_output, text):
        # only page 1 (the scanned one) should be requested for OCR
        assert "--pages" in cmd
        page_spec = cmd[cmd.index("--pages") + 1]
        assert page_spec == "1"

        out_path = tmp_path / "mixed.ocr.pdf"
        doc = fitz.open()
        _insert_image(doc.new_page(), text="OCR recovered text.")
        doc.new_page().insert_text((72, 72), "Digital page original text stays untouched.")
        doc.save(str(out_path))
        doc.close()

        class Result:
            returncode = 0
            stderr = ""

        return Result()

    monkeypatch.setattr(ocr_module.subprocess, "run", fake_run)

    blocks = await load_pdf(path, "doc-mixed", _settings())

    joined = " ".join(b.page_content for b in blocks)
    assert "OCR recovered text." in joined
    assert "Digital page original text stays untouched." in joined
