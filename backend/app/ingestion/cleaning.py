"""Cleaning pipeline: header/footer strip, de-hyphenation, whitespace, NFC, min-len drop
(T13, AC-21/AC-22/AC-23). Pure CPU — runs inline (no event loop involvement, no threading).
"""

from __future__ import annotations

import re
import unicodedata

from langchain_core.documents import Document

from app.core.settings import Settings

_DEHYPHENATE_RE = re.compile(r"(\w)-\s*\n\s*(\w)")
_INLINE_WHITESPACE_RE = re.compile(r"[ \t]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")


def _dehyphenate(text: str) -> str:
    return _DEHYPHENATE_RE.sub(r"\1\2", text)


def _collapse_whitespace(text: str) -> str:
    text = _INLINE_WHITESPACE_RE.sub(" ", text)
    text = _BLANK_LINES_RE.sub("\n\n", text)
    return "\n".join(line.strip() for line in text.split("\n")).strip()


def _find_header_footer_lines(docs: list[Document], ratio_threshold: float) -> set[str]:
    """AC-21: a line is a header/footer when it repeats verbatim across more than
    `ratio_threshold` of the *distinct pages* present. Blocks without a page (HTML/office,
    `page_start is None`) are excluded from this page-frequency computation."""
    paged = [d for d in docs if d.metadata.get("page_start") is not None]
    total_pages = len({d.metadata["page_start"] for d in paged})
    if total_pages < 2:
        return set()  # nothing to compare a "repeat" against with 0-1 pages

    pages_per_line: dict[str, set[int]] = {}
    for d in paged:
        page = d.metadata["page_start"]
        for line in d.page_content.split("\n"):
            stripped = line.strip()
            if not stripped:
                continue
            pages_per_line.setdefault(stripped, set()).add(page)

    return {
        line
        for line, pages in pages_per_line.items()
        if len(pages) >= 2 and len(pages) / total_pages > ratio_threshold
    }


def clean(docs: list[Document], settings: Settings) -> list[Document]:
    """AC-21 (header/footer strip), AC-22 (de-hyphenate, whitespace collapse, min-len drop),
    AC-23 (Unicode NFC normalize; Urdu text passes through untouched by the ASCII-only
    de-hyphenation/whitespace regexes)."""
    boilerplate_lines = _find_header_footer_lines(docs, settings.CLEAN_HEADER_FOOTER_PAGE_RATIO)

    cleaned: list[Document] = []
    for d in docs:
        text = d.page_content
        if boilerplate_lines:
            lines = [ln for ln in text.split("\n") if ln.strip() not in boilerplate_lines]
            text = "\n".join(lines)

        text = _dehyphenate(text)
        text = _collapse_whitespace(text)
        text = unicodedata.normalize("NFC", text)

        if len(text) < settings.CLEAN_MIN_BLOCK_CHARS:
            continue

        cleaned.append(Document(page_content=text, metadata=dict(d.metadata)))

    return cleaned
