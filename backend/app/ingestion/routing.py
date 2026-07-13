"""Loader routing dispatch (T7, AC-11, design.md §3).

Every returned callable has the uniform `LoaderFn` shape `(path, doc_id, settings) ->
list[Document]`, regardless of file_type — `load_office` needs a `file_type` too, so it's bound
via `functools.partial` (see `loaders/office.py`'s docstring for why the parameter is last).
Legacy `.doc`/`.ppt` re-routing (AC-20) and PDF scan/OCR handling (AC-13/AC-14) are not part of
this dispatch table: legacy detection happens before routing (`run.ingest_one`, on the
downloaded bytes) and OCR is internal to `load_pdf` itself — both keep `select_loader`'s
contract to exactly the five `sources.csv` file types (AC-3).
"""

from __future__ import annotations

from functools import partial
from pathlib import Path
from typing import Protocol

from langchain_core.documents import Document

from app.core.settings import Settings
from app.ingestion.loaders.html import load_html
from app.ingestion.loaders.office import load_office
from app.ingestion.loaders.pdf import load_pdf


class LoaderFn(Protocol):
    async def __call__(self, path: Path, doc_id: str, settings: Settings) -> list[Document]: ...


class UnknownFileTypeError(Exception):
    """AC-11: `file_type` outside the routing table."""


def select_loader(file_type: str) -> LoaderFn:
    if file_type == "pdf":
        return load_pdf
    if file_type == "html":
        return load_html
    if file_type in {"docx", "pptx", "xlsx"}:
        return partial(load_office, file_type=file_type)
    raise UnknownFileTypeError(f"no loader registered for file_type={file_type!r}")
