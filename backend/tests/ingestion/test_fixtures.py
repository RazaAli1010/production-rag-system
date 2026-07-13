"""T19: committed per-type fixtures are present and loadable; total fixture size stays small
(repo-friendly)."""

from pathlib import Path

import pytest

from app.core.settings import Settings
from app.ingestion.loaders.ocr import detect_scanned
from app.ingestion.loaders.pdf import load_pdf

_MAX_TOTAL_FIXTURE_BYTES = 2 * 1024 * 1024  # 2 MiB — generous ceiling for a "small" fixture set

EXPECTED_FIXTURES = (
    "digital.pdf",
    "scanned.pdf",
    "sample.html",
    "sample.docx",
    "sample.pptx",
    "sample.xlsx",
)


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        DATABASE_URL="postgresql+asyncpg://u:p@localhost:5432/db",
        ADMIN_EMAIL="admin@example.com",
        ADMIN_PASSWORD="secret",
        OPENAI_API_KEY="sk-test",
        PINECONE_API_KEY="pc-test",
        PINECONE_INDEX="campus-rag",
    )


def test_all_expected_fixtures_present(fixtures_dir: Path):
    for name in EXPECTED_FIXTURES:
        assert (fixtures_dir / name).exists(), f"missing fixture: {name}"


def test_fixture_set_stays_small(fixtures_dir: Path):
    total = sum((fixtures_dir / name).stat().st_size for name in EXPECTED_FIXTURES)
    assert total < _MAX_TOTAL_FIXTURE_BYTES


@pytest.mark.asyncio
async def test_digital_pdf_fixture_loads_with_text(fixtures_dir: Path):
    blocks = await load_pdf(fixtures_dir / "digital.pdf", "fixture-digital", _settings())
    assert blocks
    assert any("Academic Regulations" in b.page_content for b in blocks)


@pytest.mark.asyncio
async def test_scanned_pdf_fixture_flags_as_scanned(fixtures_dir: Path):
    report = await detect_scanned(fixtures_dir / "scanned.pdf", _settings())
    assert report.is_scanned is True
    assert report.total_pages == 2
