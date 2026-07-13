from app.core.contracts import RetrievedChunk
from app.rag import context


def _chunk(**overrides):
    defaults = dict(chunk_id="d:0", doc_id="d", title="Title", text="body",
                    section_heading="Heading", page_start=1, page_end=1, anchor=None)
    defaults.update(overrides)
    return RetrievedChunk(**defaults)


def test_format_context_numbers_in_retrieve_order():
    chunks = [_chunk(chunk_id="d:0", title="First"), _chunk(chunk_id="d:1", title="Second")]
    rendered = context.format_context(chunks)
    assert rendered.index("[1] First") < rendered.index("[2] Second")


def test_format_context_renders_page():
    chunks = [_chunk(page_start=5, page_end=5, anchor=None)]
    rendered = context.format_context(chunks)
    assert "p. 5" in rendered


def test_format_context_renders_anchor_when_no_page():
    chunks = [_chunk(page_start=None, page_end=None, anchor="section-3")]
    rendered = context.format_context(chunks)
    assert "section-3" in rendered
    assert "p." not in rendered


def test_extract_quote_under_limit_returned_verbatim():
    text = "short quote here"
    assert context.extract_quote(text, 25) == text


def test_extract_quote_truncates_to_exact_word_boundary():
    words = [f"word{i}" for i in range(40)]
    text = " ".join(words)
    quote = context.extract_quote(text, 25)
    assert quote == " ".join(words[:25])
    assert len(quote.split()) == 25
    # never mid-word: every token in the quote is a complete token from the source
    assert all(w in words for w in quote.split())
