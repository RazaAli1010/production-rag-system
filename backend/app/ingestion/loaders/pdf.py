"""PDF fast path (T8, AC-12/AC-19): PyMuPDF block extraction + reading-order sort.

Uses `fitz` (PyMuPDF) directly rather than the higher-level `PyMuPDFLoader` wrapper, because
`_reading_order_sort` (AC-19, two-column pages) needs block-level position data (`x0`/`y0`)
*before* a page's text is merged — the wrapper's default `.load()` collapses each page straight
to a single flattened string. Same underlying library/fast-path design.md §3 calls for; just
accessed at block granularity so column order can be corrected per page.
"""

from __future__ import annotations

from pathlib import Path

import anyio
import fitz  # PyMuPDF
from langchain_core.documents import Document

from app.core.settings import Settings
from app.ingestion.loaders.ocr import detect_scanned, ocr_pdf


def _extract_page_blocks(page: fitz.Page, page_no: int) -> list[Document]:
    raw_blocks = page.get_text("blocks")  # (x0, y0, x1, y1, text, block_no, block_type)
    docs: list[Document] = []
    for x0, y0, _x1, _y1, text, *_rest in raw_blocks:
        text = text.strip()
        if not text:
            continue
        docs.append(
            Document(
                page_content=text,
                metadata={"page_start": page_no, "page_end": page_no, "_x0": x0, "_y0": y0},
            )
        )
    return docs


def _reading_order_sort(blocks: list[Document]) -> list[Document]:
    """AC-19: two-column pages emit left-column blocks fully before right-column blocks.

    Heuristic: split blocks by which half of the page's block-bbox x-range they start in. If
    both halves hold >=2 blocks, treat the page as two-column and emit left-sorted-top-to-bottom
    then right-sorted-top-to-bottom; otherwise fall back to a single top-to-bottom order (the
    common single-column case, where the split would be spurious).
    """
    if not blocks:
        return []
    x0s = [b.metadata["_x0"] for b in blocks]
    midpoint = (min(x0s) + max(x0s)) / 2
    left = [b for b in blocks if b.metadata["_x0"] < midpoint]
    right = [b for b in blocks if b.metadata["_x0"] >= midpoint]

    if len(left) >= 2 and len(right) >= 2:
        left.sort(key=lambda b: b.metadata["_y0"])
        right.sort(key=lambda b: b.metadata["_y0"])
        return left + right

    return sorted(blocks, key=lambda b: (b.metadata["_y0"], b.metadata["_x0"]))


def _extract_sync(path: Path, doc_id: str) -> list[Document]:
    result: list[Document] = []
    with fitz.open(str(path)) as pdf:
        for page_no, page in enumerate(pdf, start=1):
            for block in _reading_order_sort(_extract_page_blocks(page, page_no)):
                block.metadata.pop("_x0", None)
                block.metadata.pop("_y0", None)
                block.metadata["doc_id"] = doc_id
                result.append(block)
    return result


async def load_pdf(path: Path, doc_id: str, settings: Settings) -> list[Document]:
    """AC-12: PyMuPDF fast path; page-accurate `page_start`/`page_end` on every block.

    AC-13/AC-14: also runs scan-detection first and, if the document (or any of its pages) is
    scanned, OCRs just the scanned pages and re-loads via the same fast path — the digital vs.
    scanned vs. mixed cases are transparent to `routing.select_loader("pdf")`'s single callable.
    Every returned block carries a transient `is_scanned` metadata key (doc-level flag) that
    `run.ingest_one` reads for the `documents.is_scanned` column; `serialize.write_jsonl` strips
    it before the F1/F2 JSONL handoff (AC-25 — only chunk-independent, citation metadata survives).
    """
    scan_report = await detect_scanned(path, settings)

    effective_path = path
    if scan_report.is_scanned:
        effective_path = await ocr_pdf(path, scan_report.scanned_pages, settings)

    blocks = await anyio.to_thread.run_sync(_extract_sync, effective_path, doc_id)
    for block in blocks:
        block.metadata["is_scanned"] = scan_report.is_scanned
    return blocks
