import app.db.models.corpus as corpus
from app.core.contracts import RetrievedChunk
from app.rag.citations import _extract_marker_numbers, parse_citations
from app.rag.context import extract_quote


async def _seed(session, n=3):
    doc = corpus.Document(doc_id="d", title="Doc Title", source_org="PU", url="http://x",
                          file_type="pdf", version_label="v1", is_scanned=False, page_count=10)
    session.add(doc)
    await session.flush()  # document must exist before chunks (FK) — no relationship() defined
                            # between the two models, so flush ordering isn't inferred for us
    for i in range(n):
        session.add(corpus.Chunk(
            chunk_id=f"d:{i}", doc_id="d", seq=i,
            text=" ".join(f"word{i}_{w}" for w in range(40)),  # 40 words, forces truncation
            section_heading=f"Section {i}", page_start=i + 1, page_end=i + 1,
            anchor=None, token_count=40,
        ))
    await session.flush()


def _retrieved(i):
    return RetrievedChunk(chunk_id=f"d:{i}", doc_id="d", title="Doc Title", text=f"stub {i}",
                          dense_score=0.9)


class _SpySession:
    """Counts `.execute()` calls while delegating to a real AsyncSession — used to assert F3's
    citation resolution is exactly one batched query, never one-per-marker (AC-15)."""

    def __init__(self, real_session):
        self._real = real_session
        self.execute_calls = 0

    async def execute(self, *a, **kw):
        self.execute_calls += 1
        return await self._real.execute(*a, **kw)


def test_extract_marker_numbers_distinct_ordered_and_bounded():
    assert _extract_marker_numbers("[1] then [3] then [1] again [9]", n_chunks=3) == [1, 3]


async def test_parse_citations_resolves_valid_markers_in_one_query(session):
    await _seed(session, n=3)
    chunks = [_retrieved(0), _retrieved(1), _retrieved(2)]
    spy = _SpySession(session)

    citations = await parse_citations("Cites [1] and [3] but not [9].", chunks, spy)

    assert spy.execute_calls == 1
    assert {c.chunk_id for c in citations} == {"d:0", "d:2"}
    assert all(c.doc_id == "d" and c.title == "Doc Title" for c in citations)


async def test_parse_citations_quote_matches_extract_quote_of_stored_text(session):
    await _seed(session, n=1)
    chunks = [_retrieved(0)]
    citations = await parse_citations("See [1].", chunks, session)

    assert len(citations) == 1
    stored_text = " ".join(f"word0_{w}" for w in range(40))
    assert citations[0].quote == extract_quote(stored_text, 25)
    assert len(citations[0].quote.split()) == 25


async def test_parse_citations_zero_markers_returns_empty_list(session):
    await _seed(session, n=1)
    citations = await parse_citations("No markers here.", [_retrieved(0)], session)
    assert citations == []
