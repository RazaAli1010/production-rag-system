import pytest

from app.core.settings import Settings
from app.indexing.chunkers.base import (
    count_tokens,
    make_chunk_id,
    select_chunker,
    truncate_to_limit,
)


def _settings(**o):
    return Settings(_env_file=None, DATABASE_URL="postgresql+asyncpg://u:p@localhost:5432/db",
                    ADMIN_EMAIL="a@b.c", ADMIN_PASSWORD="x", OPENAI_API_KEY="k",
                    PINECONE_API_KEY="k", PINECONE_INDEX="i", **o)


def test_count_tokens_nonzero():
    assert count_tokens("hello world") >= 2


def test_make_chunk_id():
    assert make_chunk_id("hec-x-2021", 3) == "hec-x-2021:3"


def test_truncate_under_limit_passthrough():
    s = _settings(EMBED_MAX_CHUNK_TOKENS=8000)
    text, tc = truncate_to_limit("short", s)
    assert text == "short" and tc == count_tokens("short")


def test_truncate_over_limit():
    s = _settings(EMBED_MAX_CHUNK_TOKENS=5)
    text, tc = truncate_to_limit("word " * 50, s)
    assert tc == 5 and count_tokens(text) == 5


def test_select_chunker():
    s = _settings()
    from app.indexing.chunkers.fixed import FixedChunker
    from app.indexing.chunkers.structure import StructureChunker
    assert isinstance(select_chunker("fixed", s), FixedChunker)
    assert isinstance(select_chunker("structure", s), StructureChunker)
    with pytest.raises(ValueError):
        select_chunker("bogus", s)
