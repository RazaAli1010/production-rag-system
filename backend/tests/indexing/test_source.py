import json

import structlog

from app.indexing.source import load_blocks
from app.core.settings import Settings


def _settings(tmp_path):
    return Settings(_env_file=None, DATABASE_URL="postgresql+asyncpg://u:p@localhost:5432/db",
                    ADMIN_EMAIL="a@b.c", ADMIN_PASSWORD="x", OPENAI_API_KEY="k",
                    PINECONE_API_KEY="k", PINECONE_INDEX="i", EXTRACTED_DIR=tmp_path)


async def test_load_blocks_reads_documents(tmp_path):
    s = _settings(tmp_path)
    line = {"page_content": "Block one.", "metadata": {"doc_id": "d1", "page_start": 1,
            "page_end": 1, "anchor": None, "section_heading": "Intro"}}
    (tmp_path / "d1.jsonl").write_text(json.dumps(line) + "\n", encoding="utf-8")
    docs = await load_blocks("d1", s)
    assert len(docs) == 1
    assert docs[0].page_content == "Block one."
    assert docs[0].metadata["section_heading"] == "Intro"
    assert docs[0].metadata["page_start"] == 1


async def test_load_blocks_missing_file_warns_returns_empty(tmp_path):
    s = _settings(tmp_path)
    with structlog.testing.capture_logs() as logs:
        docs = await load_blocks("nope", s)
    assert docs == []
    assert any(e["event"] == "indexing.source.missing" for e in logs)
