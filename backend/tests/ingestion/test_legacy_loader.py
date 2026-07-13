"""T12: `.doc` fixture converts and extracts (mocked libreoffice); forced conversion failure
marks `failed` unsupported (AC-20)."""

import pytest

from app.core.settings import Settings
from app.ingestion.loaders import legacy as legacy_module
from app.ingestion.loaders.legacy import (
    LegacyConversionError,
    convert_legacy,
    is_legacy_office_binary,
)


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        DATABASE_URL="postgresql+asyncpg://u:p@localhost:5432/db",
        ADMIN_EMAIL="admin@example.com",
        ADMIN_PASSWORD="secret",
    )


@pytest.mark.asyncio
async def test_is_legacy_office_binary_true_for_ole2_signature(tmp_path):
    path = tmp_path / "old.doc"
    path.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"rest of legacy binary content")

    assert await is_legacy_office_binary(path) is True


@pytest.mark.asyncio
async def test_is_legacy_office_binary_false_for_ooxml(tmp_path):
    path = tmp_path / "modern.docx"
    path.write_bytes(b"PK\x03\x04rest of a zip-based ooxml file")

    assert await is_legacy_office_binary(path) is False


@pytest.mark.asyncio
async def test_convert_legacy_doc_success(tmp_path, monkeypatch):
    path = tmp_path / "old.doc"
    path.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1fake legacy doc bytes")

    def fake_run(cmd, capture_output, text):
        assert cmd[0] == "libreoffice"
        assert "--convert-to" in cmd
        out_path = tmp_path / "old.docx"
        out_path.write_bytes(b"PK\x03\x04converted docx bytes")

        class Result:
            returncode = 0
            stderr = ""

        return Result()

    monkeypatch.setattr(legacy_module.subprocess, "run", fake_run)

    converted_path, file_type = await convert_legacy(path, _settings())

    assert file_type == "docx"
    assert converted_path.name == "old.docx"
    assert converted_path.exists()


@pytest.mark.asyncio
async def test_convert_legacy_failure_marks_unsupported(tmp_path, monkeypatch):
    path = tmp_path / "old.ppt"
    path.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1fake legacy ppt bytes")

    def fake_run(cmd, capture_output, text):
        class Result:
            returncode = 1
            stderr = "soffice: command not found"

        return Result()

    monkeypatch.setattr(legacy_module.subprocess, "run", fake_run)

    with pytest.raises(LegacyConversionError, match="command not found"):
        await convert_legacy(path, _settings())


@pytest.mark.asyncio
async def test_convert_legacy_missing_output_raises(tmp_path, monkeypatch):
    path = tmp_path / "old.doc"
    path.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1fake legacy doc bytes")

    def fake_run(cmd, capture_output, text):
        class Result:  # reports success but doesn't actually write the output file
            returncode = 0
            stderr = ""

        return Result()

    monkeypatch.setattr(legacy_module.subprocess, "run", fake_run)

    with pytest.raises(LegacyConversionError, match="was not created"):
        await convert_legacy(path, _settings())
