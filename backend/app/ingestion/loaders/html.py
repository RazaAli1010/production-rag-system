"""HTML loader: nav/boilerplate stripped, heading anchors captured (T10, AC-15, AC-32).

Implemented directly on BeautifulSoup rather than `UnstructuredHTMLLoader`/`BSHTMLLoader`
(design.md §3), for the same reason `loaders/pdf.py` bypasses the `PyMuPDFLoader` wrapper: both
of those collapse a page to a single flattened text blob before we'd get a chance to capture
per-heading `id` attributes into `anchor` (AC-15) or strip `<nav>`/`<header>`/`<footer>` at the
tag level. BeautifulSoup is the library both wrappers use internally, so this is the same
parsing engine, used at finer (per-block) granularity.
"""

from __future__ import annotations

import re
from pathlib import Path

import aiofiles
import anyio
from bs4 import BeautifulSoup
from langchain_core.documents import Document

from app.core.settings import Settings

_BOILERPLATE_TAGS = ("nav", "header", "footer", "script", "style", "aside", "noscript")
_HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")
_CONTENT_TAGS = ("p", "li", "td", "th", "blockquote", "pre", "figcaption")
_LINK_ONLY_TEXT_THRESHOLD = 200  # visible chars; below this + a PDF link => suggest the PDF


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "section"


def _parse_sync(html: str, doc_id: str) -> list[Document]:
    soup = BeautifulSoup(html, "lxml")

    for tag_name in _BOILERPLATE_TAGS:
        for tag in soup.find_all(tag_name):
            tag.decompose()

    body = soup.body or soup

    blocks: list[Document] = []
    current_anchor: str | None = None
    current_heading: str | None = None
    seen_slugs: set[str] = set()

    for el in body.find_all(list(_HEADING_TAGS) + list(_CONTENT_TAGS)):
        if el.name in _HEADING_TAGS:
            text = el.get_text(" ", strip=True)
            if not text:
                continue
            anchor = el.get("id") or slugify(text)
            base, i = anchor, 2
            while anchor in seen_slugs:  # de-dupe generated slugs for repeated headings
                anchor = f"{base}-{i}"
                i += 1
            seen_slugs.add(anchor)
            current_anchor = anchor
            current_heading = text
            continue

        text = el.get_text(" ", strip=True)
        if not text:
            continue
        blocks.append(
            Document(
                page_content=text,
                metadata={
                    "doc_id": doc_id,
                    "anchor": current_anchor,
                    "section_heading": current_heading,
                },
            )
        )

    visible_text_len = sum(len(b.page_content) for b in blocks)
    pdf_links = [
        a["href"] for a in body.find_all("a", href=True) if a["href"].lower().endswith(".pdf")
    ]
    links_only_pdf = bool(pdf_links) and visible_text_len < _LINK_ONLY_TEXT_THRESHOLD

    if not blocks and pdf_links:
        # nothing but a PDF link — still emit one block so the report flag has a carrier.
        blocks.append(
            Document(
                page_content=f"[This page links a PDF: {pdf_links[0]}]",
                metadata={"doc_id": doc_id, "anchor": None, "section_heading": None},
            )
        )

    for block in blocks:
        block.metadata["html_links_only_pdf"] = links_only_pdf

    return blocks


async def load_html(path: Path, doc_id: str, settings: Settings) -> list[Document]:
    """AC-15: strip nav/boilerplate, capture heading anchors; page fields stay `None` (HTML has
    no page concept). Also flags pages that merely link a PDF for the run report (AC-32) via a
    transient `html_links_only_pdf` metadata key (`settings` unused; kept for a uniform
    `LoaderFn` signature across `routing.select_loader`)."""
    async with aiofiles.open(path, encoding="utf-8", errors="replace") as f:
        html = await f.read()
    return await anyio.to_thread.run_sync(_parse_sync, html, doc_id)
