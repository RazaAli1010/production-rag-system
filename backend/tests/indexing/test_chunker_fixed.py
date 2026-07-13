from langchain_core.documents import Document

from app.core.settings import Settings
from app.indexing.chunkers.base import count_tokens
from app.indexing.chunkers.fixed import FixedChunker


def _settings(**o):
    return Settings(_env_file=None, DATABASE_URL="postgresql+asyncpg://u:p@localhost:5432/db",
                    ADMIN_EMAIL="a@b.c", ADMIN_PASSWORD="x", OPENAI_API_KEY="k",
                    PINECONE_API_KEY="k", PINECONE_INDEX="i", **o)


def _doc(text, **md):
    base = {"doc_id": "d", "page_start": 2, "page_end": 2, "anchor": None, "section_heading": "H"}
    base.update(md)
    return Document(page_content=text, metadata=base)


def test_long_block_splits_under_limit_with_metadata():
    s = _settings()
    long_text = "clause text. " * 400
    chunks = FixedChunker(s).split([_doc(long_text)], "d")
    assert len(chunks) > 1
    assert all(c.token_count <= s.FIXED_CHUNK_TOKENS for c in chunks)
    assert [c.seq for c in chunks] == list(range(len(chunks)))
    assert all(c.chunk_id == f"d:{c.seq}" for c in chunks)
    assert all(c.page_start == 2 and c.section_heading == "H" for c in chunks)


def test_seq_contiguous_across_blocks():
    s = _settings()
    chunks = FixedChunker(s).split([_doc("a b c"), _doc("d e f")], "d")
    assert [c.seq for c in chunks] == list(range(len(chunks)))


def test_oversize_single_chunk_truncated(monkeypatch):
    s = _settings(FIXED_CHUNK_TOKENS=100000, FIXED_CHUNK_OVERLAP=0, EMBED_MAX_CHUNK_TOKENS=20)
    chunks = FixedChunker(s).split([_doc("token " * 200)], "d")
    assert all(count_tokens(c.text) <= 20 for c in chunks)
