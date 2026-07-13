"""T8: PyMuPDF fast path yields page-accurate blocks; two-column pages emit left-column-before-
right-column (AC-12, AC-19)."""

import fitz
import pytest
from langchain_core.documents import Document

from app.core.settings import Settings
from app.ingestion.loaders.pdf import _reading_order_sort, load_pdf


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


def _make_single_column_pdf(path):
    doc = fitz.open()
    p1 = doc.new_page()
    p1.insert_text((72, 72), "Page one content.")
    p2 = doc.new_page()
    p2.insert_text((72, 72), "Page two content.")
    doc.save(str(path))
    doc.close()


def _make_two_column_pdf(path):
    """Two clearly-separated column boxes, each with several distinct lines, so PyMuPDF's block
    detector keeps them as separate blocks (a same-y single-line layout gets fused into one
    block by PyMuPDF's layout analysis, which would defeat this test)."""
    doc = fitz.open()
    page = doc.new_page()
    left_rect = fitz.Rect(36, 72, 280, 400)
    right_rect = fitz.Rect(320, 72, 560, 400)
    page.insert_textbox(left_rect, "Left A.\nLeft B.\nLeft C.\nLeft D.")
    page.insert_textbox(right_rect, "Right A.\nRight B.\nRight C.\nRight D.")
    doc.save(str(path))
    doc.close()


@pytest.mark.asyncio
async def test_digital_pdf_yields_page_accurate_blocks(tmp_path):
    pdf_path = tmp_path / "digital.pdf"
    _make_single_column_pdf(pdf_path)

    docs = await load_pdf(pdf_path, "doc-1", _settings())

    pages = {d.metadata["page_start"] for d in docs}
    assert pages == {1, 2}
    for d in docs:
        assert d.metadata["page_start"] == d.metadata["page_end"]
        assert d.metadata["doc_id"] == "doc-1"
    contents = " ".join(d.page_content for d in docs)
    assert "Page one content." in contents
    assert "Page two content." in contents


@pytest.mark.asyncio
async def test_two_column_pdf_emits_left_before_right(tmp_path):
    pdf_path = tmp_path / "two_col.pdf"
    _make_two_column_pdf(pdf_path)

    docs = await load_pdf(pdf_path, "doc-2", _settings())

    joined = "\n".join(d.page_content for d in docs)
    assert joined.index("Left A.") < joined.index("Right A.")
    # every left-column line appears before every right-column line
    last_left_idx = max(joined.index(f"Left {c}.") for c in "ABCD")
    first_right_idx = min(joined.index(f"Right {c}.") for c in "ABCD")
    assert last_left_idx < first_right_idx


def test_reading_order_sort_single_column_falls_back_to_top_to_bottom():
    blocks = [
        Document(page_content="second", metadata={"_x0": 10, "_y0": 50}),
        Document(page_content="first", metadata={"_x0": 10, "_y0": 10}),
    ]
    ordered = _reading_order_sort(blocks)
    assert [d.page_content for d in ordered] == ["first", "second"]


def test_reading_order_sort_empty_returns_empty():
    assert _reading_order_sort([]) == []
