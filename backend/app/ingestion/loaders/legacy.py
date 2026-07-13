"""Legacy `.doc`/`.ppt` conversion via `libreoffice --headless` (T12, AC-20).

`libreoffice` is invoked as an external CLI subprocess (design.md §4: "subprocess -> to_thread")
— not a Python-importable dependency of this project; only the Docker ingestion image has it
installed. Tests mock `subprocess.run` at this module's boundary.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import aiofiles
import anyio

from app.core.settings import Settings

_LEGACY_TARGET = {".doc": "docx", ".ppt": "pptx"}
_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


class LegacyConversionError(Exception):
    """`libreoffice --headless` exited non-zero (or didn't produce the expected file) — caller
    marks the document `failed` as unsupported (AC-20)."""


async def is_legacy_office_binary(path: Path) -> bool:
    """OLE2 signature => the downloaded bytes are the legacy binary `.doc`/`.ppt` format even
    though `file_type` declared `docx`/`pptx` (AC-8's sniff treats this as a valid variant, not
    a mismatch — see `downloader.sniff_content_type`); `run.ingest_one` uses this to decide
    whether to re-route through `convert_legacy` before the normal office loader."""
    async with aiofiles.open(path, "rb") as f:
        head = await f.read(8)
    return head.startswith(_OLE2_MAGIC)


def _convert_sync(path: Path, binary: str) -> tuple[Path, str]:
    suffix = path.suffix.lower()
    target_ext = _LEGACY_TARGET.get(suffix)
    if target_ext is None:
        raise LegacyConversionError(f"no legacy conversion target for suffix {suffix!r}")

    result = subprocess.run(
        [binary, "--headless", "--convert-to", target_ext, "--outdir", str(path.parent), str(path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise LegacyConversionError(
            f"libreoffice --headless exited {result.returncode}: {result.stderr}"
        )

    converted_path = path.with_suffix(f".{target_ext}")
    if not converted_path.exists():
        raise LegacyConversionError(
            f"libreoffice reported success but {converted_path} was not created"
        )
    return converted_path, target_ext


async def convert_legacy(path: Path, settings: Settings) -> tuple[Path, str]:
    """AC-20: `.doc`/`.ppt` -> `.docx`/`.pptx` via `libreoffice --headless`; on success the
    caller re-routes through `routing.select_loader(file_type)` using the returned `file_type`."""
    return await anyio.to_thread.run_sync(_convert_sync, path, settings.LIBREOFFICE_BIN)
