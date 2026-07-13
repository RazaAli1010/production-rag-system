"""T13: repeated footer removed; hyphen-split word rejoined; Urdu preserved + NFC-normalized;
short blocks dropped (AC-21/AC-22/AC-23)."""

import unicodedata

from langchain_core.documents import Document

from app.core.settings import Settings
from app.ingestion.cleaning import clean


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


def test_repeated_footer_stripped_across_pages():
    footer = "University of the Punjab - Confidential"
    docs = [
        Document(
            page_content=f"Body text unique to page {i} describing regulations.\n{footer}",
            metadata={"page_start": i, "page_end": i},
        )
        for i in range(1, 6)
    ]
    cleaned = clean(docs, _settings())

    assert len(cleaned) == 5
    for d in cleaned:
        assert footer not in d.page_content
        assert "describing regulations" in d.page_content


def test_unique_lines_not_treated_as_footer():
    docs = [
        Document(
            page_content=f"Unique content line number {i} about examinations and probation rules.",
            metadata={"page_start": i, "page_end": i},
        )
        for i in range(1, 6)
    ]
    cleaned = clean(docs, _settings())
    assert len(cleaned) == 5
    for i, d in enumerate(cleaned, start=1):
        assert f"line number {i}" in d.page_content


def test_dehyphenation_rejoins_split_word():
    docs = [
        Document(
            page_content="This is a long regu-\nlation about academic probation for students.",
            metadata={"page_start": 1, "page_end": 1},
        )
    ]
    cleaned = clean(docs, _settings())
    assert len(cleaned) == 1
    assert "regulation" in cleaned[0].page_content
    assert "regu-" not in cleaned[0].page_content


def test_urdu_text_preserved_and_nfc_normalized():
    urdu_text = "امتحان میں ناکامی کی صورت میں پروبیشن کا اطلاق ہوگا۔"
    decomposed = unicodedata.normalize("NFD", urdu_text)
    docs = [Document(page_content=decomposed, metadata={"page_start": None, "page_end": None})]

    cleaned = clean(docs, _settings())

    assert len(cleaned) == 1
    assert cleaned[0].page_content == unicodedata.normalize("NFC", urdu_text)


def test_short_blocks_dropped():
    docs = [
        Document(page_content="Too short", metadata={"page_start": None}),
        Document(
            page_content="This block has plenty of characters to survive the min-length filter.",
            metadata={"page_start": None},
        ),
    ]
    cleaned = clean(docs, _settings())
    assert len(cleaned) == 1
    assert "plenty of characters" in cleaned[0].page_content


def test_whitespace_collapsed():
    docs = [
        Document(
            page_content="Too    many     spaces   and\t\ttabs   in this line of text here.",
            metadata={"page_start": None},
        )
    ]
    cleaned = clean(docs, _settings())
    assert "  " not in cleaned[0].page_content
    assert "\t" not in cleaned[0].page_content
