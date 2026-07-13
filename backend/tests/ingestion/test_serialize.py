"""T14: JSONL written; each line valid JSON with required metadata keys; no chunk fields
present (AC-24/AC-25)."""

import json

import pytest
from langchain_core.documents import Document

from app.core.settings import Settings
from app.ingestion.serialize import write_jsonl


def _settings(extracted_dir) -> Settings:
    s = Settings(
        _env_file=None,
        DATABASE_URL="postgresql+asyncpg://u:p@localhost:5432/db",
        ADMIN_EMAIL="admin@example.com",
        ADMIN_PASSWORD="secret",
        OPENAI_API_KEY="sk-test",
        PINECONE_API_KEY="pc-test",
        PINECONE_INDEX="campus-rag",
    )
    s.EXTRACTED_DIR = extracted_dir
    return s


@pytest.mark.asyncio
async def test_write_jsonl_creates_valid_lines_with_required_keys(tmp_path):
    docs = [
        Document(
            page_content="Block one text.",
            metadata={
                "doc_id": "doc-1", "page_start": 1, "page_end": 1,
                "anchor": None, "section_heading": "Intro",
                "is_scanned": False,  # transient — must not survive to the JSONL
            },
        ),
        Document(
            page_content="Block two text.",
            metadata={"doc_id": "doc-1", "page_start": None, "page_end": None, "anchor": "sec-1"},
        ),
    ]

    out_path = await write_jsonl("doc-1", docs, _settings(tmp_path / "extracted"))

    assert out_path.exists()
    assert out_path.name == "doc-1.jsonl"

    lines = out_path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2

    for line in lines:
        obj = json.loads(line)  # each line must be valid, standalone JSON
        assert set(obj.keys()) == {"page_content", "metadata"}
        assert set(obj["metadata"].keys()) == {
            "doc_id", "page_start", "page_end", "anchor", "section_heading",
        }
        assert "chunk_id" not in obj["metadata"]
        assert "seq" not in obj["metadata"]
        assert "is_scanned" not in obj["metadata"]

    first = json.loads(lines[0])
    assert first["metadata"]["doc_id"] == "doc-1"
    assert first["metadata"]["section_heading"] == "Intro"


@pytest.mark.asyncio
async def test_write_jsonl_empty_docs_creates_empty_file(tmp_path):
    out_path = await write_jsonl("doc-empty", [], _settings(tmp_path / "extracted"))
    assert out_path.exists()
    assert out_path.read_text(encoding="utf-8") == ""
