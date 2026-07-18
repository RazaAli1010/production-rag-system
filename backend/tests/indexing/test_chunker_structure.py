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
    # Budget below the combined size so _pack cannot merge the two — this test is about the
    # heading split, which packing is layered on top of.
    s = _settings(FIXED_CHUNK_TOKENS=8, FIXED_CHUNK_OVERLAP=0)
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


def test_all_sections_short_still_emits_content():
    s = _settings(CLEAN_MIN_BLOCK_CHARS=20)
    docs = [_block("hi", heading="A"), _block("yo", heading="B")]
    chunks = StructureChunker(s).split(docs, "d")
    assert len(chunks) == 1
    assert chunks[0].section_heading == "B"
    assert "hi" in chunks[0].text and "yo" in chunks[0].text


def test_pdf_clause_regex_opens_sections():
    # too small to pack; see test_html_splits_on_heading
    s = _settings(FIXED_CHUNK_TOKENS=8, FIXED_CHUNK_OVERLAP=0)
    body = "Preamble line.\n1. First clause body.\n2. Second clause body.\n"
    chunks = StructureChunker(s).split([_block(body, page=5)], "d")
    assert len(chunks) >= 2
    assert all(c.page_start == 5 for c in chunks)


def test_packs_small_sections_up_to_the_budget():
    """The extractor emits one block per line; unpacked, every clause is its own tiny chunk."""
    s = _settings(FIXED_CHUNK_TOKENS=1000)
    docs = [_block(f"Clause {i} body text of a regulation.", heading=f"S{i}", page=i)
            for i in range(1, 21)]
    chunks = StructureChunker(s).split(docs, "d")

    assert len(chunks) == 1, "20 tiny sections fit in one 1000-token chunk"
    assert chunks[0].token_count <= s.FIXED_CHUNK_TOKENS
    assert "Clause 1 " in chunks[0].text and "Clause 20 " in chunks[0].text
    # Citations stay usable: the page range spans what was packed in.
    assert (chunks[0].page_start, chunks[0].page_end) == (1, 20)


def test_pack_never_exceeds_the_budget():
    s = _settings(FIXED_CHUNK_TOKENS=40, FIXED_CHUNK_OVERLAP=0)
    docs = [_block(f"Clause {i} body text of a regulation here.", heading=f"S{i}")
            for i in range(30)]
    chunks = StructureChunker(s).split(docs, "d")
    assert len(chunks) > 1
    assert all(c.token_count <= s.FIXED_CHUNK_TOKENS for c in chunks)
    assert [c.seq for c in chunks] == list(range(len(chunks)))


def test_oversize_section_resplit_keeps_parent_heading():
    s = _settings(STRUCTURE_MAX_SECTION_TOKENS=50, FIXED_CHUNK_TOKENS=50, FIXED_CHUNK_OVERLAP=0)
    big = "word " * 400
    chunks = StructureChunker(s).split([_block(big, heading="Parent")], "d")
    assert len(chunks) > 1
    assert all(c.section_heading == "Parent" for c in chunks)
    assert all(c.token_count <= s.FIXED_CHUNK_TOKENS for c in chunks)
