"""Scanned-PDF detection + selective OCR (T9, AC-13/AC-14).

`ocrmypdf` is invoked as an external CLI subprocess (design.md §4: "subprocess -> to_thread") —
not a Python-importable dependency of this project; only the Docker ingestion image has the
`ocrmypdf`/Tesseract binaries installed locally. Tests mock `subprocess.run` at this module's
boundary; detection (pure PyMuPDF, no external binary) is exercised for real against fixtures.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import anyio
import fitz  # PyMuPDF

from app.core.settings import Settings
from app.ingestion.schemas import ScanReport


class OCRFailedError(Exception):
    """`ocrmypdf` exited non-zero — caller marks the document `failed` with a tool note."""


def _detect_scanned_sync(path: Path, min_chars: int, threshold: float) -> ScanReport:
    scanned_pages: list[int] = []
    with fitz.open(str(path)) as pdf:
        total_pages = pdf.page_count
        for page_no, page in enumerate(pdf, start=1):
            text_len = len(page.get_text("text").strip())
            has_image = len(page.get_images(full=True)) >= 1
            if text_len < min_chars and has_image:
                scanned_pages.append(page_no)
    ratio = (len(scanned_pages) / total_pages) if total_pages else 0.0
    return ScanReport(
        is_scanned=ratio > threshold,
        scanned_pages=scanned_pages,
        total_pages=total_pages,
        scanned_ratio=ratio,
    )


async def detect_scanned(path: Path, settings: Settings) -> ScanReport:
    """AC-13: a page is scanned when it has < `OCR_MIN_PAGE_TEXT_CHARS` extractable text AND
    >=1 image XObject; doc-level `is_scanned` when that holds for
    > `OCR_SCANNED_PAGE_THRESHOLD` of pages."""
    return await anyio.to_thread.run_sync(
        _detect_scanned_sync, path, settings.OCR_MIN_PAGE_TEXT_CHARS,
        settings.OCR_SCANNED_PAGE_THRESHOLD,
    )


def _ocr_sync(path: Path, pages: list[int], languages: str) -> Path:
    output_path = path.with_name(f"{path.stem}.ocr{path.suffix}")
    page_spec = ",".join(str(p) for p in pages)
    result = subprocess.run(
        ["ocrmypdf", "-l", languages, "--pages", page_spec, str(path), str(output_path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise OCRFailedError(f"ocrmypdf exited {result.returncode}: {result.stderr}")
    return output_path


async def ocr_pdf(path: Path, pages: list[int], settings: Settings) -> Path:
    """AC-13/AC-14: OCR only `pages` (1-indexed, selective). `ocrmypdf --pages` leaves every
    other page's original content untouched, so a mixed PDF's digital pages keep their original
    text automatically — no separate merge step needed."""
    return await anyio.to_thread.run_sync(_ocr_sync, path, pages, settings.OCR_LANGUAGES)
