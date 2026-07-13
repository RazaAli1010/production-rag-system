from langchain_core.documents import Document

from app.core.settings import Settings
from app.indexing.chunkers.structure import StructureChunker


def _settings(**o):
    return Settings(_env_file=None, DATABASE_URL="postgresql+asyncpg://u:p@localhost:5432/db",
                    ADMIN_EMAIL="a@b.c", ADMIN_PASSWORD="x", OPENAI_API_KEY="k",
                    PINECONE_API_KEY="k", PINECONE_INDEX="i", **o)


def _block(text, heading=None, anchor=None, page=None):
    return Document(page_content=text, metadata={"doc_id": "d", "page_start": page,
                    "page_end": page, "anchor": anchor, "section_heading": heading})


def test_html_splits_on_heading():
    s = _settings()
    docs = [_block("This is the introduction body text.", heading="Introduction", anchor="intro"),
            _block("This is the rules body text.", heading="Rules", anchor="rules")]
    chunks = StructureChunker(s).split(docs, "d")
    headings = {c.section_heading for c in chunks}
    assert headings == {"Introduction", "Rules"}
    assert [c.seq for c in chunks] == list(range(len(chunks)))


def test_empty_section_merges_forward():
    s = _settings(CLEAN_MIN_BLOCK_CHARS=20)
    docs = [_block("hi", heading="Tiny"),
            _block("A real body of sufficient length here.", heading="Big")]
    chunks = StructureChunker(s).split(docs, "d")
    assert all(c.text.strip() for c in chunks)
    assert len(chunks) == 1


def test_pdf_clause_regex_opens_sections():
    s = _settings()
    body = "Preamble line.\n1. First clause body.\n2. Second clause body.\n"
    chunks = StructureChunker(s).split([_block(body, page=5)], "d")
    assert len(chunks) >= 2
    assert all(c.page_start == 5 for c in chunks)


def test_oversize_section_resplit_keeps_parent_heading():
    s = _settings(STRUCTURE_MAX_SECTION_TOKENS=50, FIXED_CHUNK_TOKENS=50, FIXED_CHUNK_OVERLAP=0)
    big = "word " * 400
    chunks = StructureChunker(s).split([_block(big, heading="Parent")], "d")
    assert len(chunks) > 1
    assert all(c.section_heading == "Parent" for c in chunks)
    assert all(c.token_count <= s.FIXED_CHUNK_TOKENS for c in chunks)
