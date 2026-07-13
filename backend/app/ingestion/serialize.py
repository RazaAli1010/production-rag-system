"""JSONL serialization — the F1/F2 seam (T14, AC-24/AC-25).

`data/extracted/{doc_id}.jsonl` is the stable contract F2 reads: one JSON object per line,
`{"page_content": str, "metadata": {...}}`, metadata restricted to the chunk-independent
citation fields. `chunk_id`/`seq` are never written here (AC-25 — those are F2's job), and any
transient loader-internal metadata (`is_scanned`, `html_links_only_pdf` — read by
`run.ingest_one` for the `documents` row / run report, not part of the F1/F2 contract) is
dropped at this boundary.
"""

from __future__ import annotations

import json
from pathlib import Path

import aiofiles
from langchain_core.documents import Document

from app.core.settings import Settings

CANONICAL_METADATA_KEYS = ("doc_id", "page_start", "page_end", "anchor", "section_heading")


def _to_json_line(doc: Document) -> str:
    metadata = {k: doc.metadata.get(k) for k in CANONICAL_METADATA_KEYS}
    return json.dumps({"page_content": doc.page_content, "metadata": metadata}, ensure_ascii=False)


async def write_jsonl(doc_id: str, docs: list[Document], settings: Settings) -> Path:
    """AC-24: write all cleaned blocks to `data/extracted/{doc_id}.jsonl` via `aiofiles`."""
    settings.EXTRACTED_DIR.mkdir(parents=True, exist_ok=True)
    out_path = settings.EXTRACTED_DIR / f"{doc_id}.jsonl"

    body = "".join(_to_json_line(d) + "\n" for d in docs)
    async with aiofiles.open(out_path, "w", encoding="utf-8") as f:
        await f.write(body)

    return out_path
