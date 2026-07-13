from app.core.contracts import Citation, RetrievedChunk
from app.core.settings import Settings
from app.rag import refusal


def _settings(**o):
    return Settings(
        _env_file=None,
        DATABASE_URL="postgresql+asyncpg://u:p@localhost:5432/db",
        ADMIN_EMAIL="a@b.c",
        ADMIN_PASSWORD="x",
        OPENAI_API_KEY="k",
        PINECONE_API_KEY="k",
        PINECONE_INDEX="i",
        **o,
    )


def _chunk(doc_id, score, text="chunk body text here"):
    return RetrievedChunk(chunk_id=f"{doc_id}:0", doc_id=doc_id, title=f"Title {doc_id}",
                          text=text, dense_score=score)


def test_pre_llm_gate_fires_below_threshold():
    settings = _settings(REFUSAL_DENSE_THRESHOLD=0.25)
    chunks = [_chunk("d1", 0.1)]
    assert refusal.pre_llm_gate(chunks, settings) is True


def test_pre_llm_gate_does_not_fire_above_threshold():
    settings = _settings(REFUSAL_DENSE_THRESHOLD=0.25)
    chunks = [_chunk("d1", 0.9)]
    assert refusal.pre_llm_gate(chunks, settings) is False


def test_pre_llm_gate_fires_on_empty_retrieval():
    settings = _settings(REFUSAL_DENSE_THRESHOLD=0.25)
    assert refusal.pre_llm_gate([], settings) is True


def test_suggestion_citations_caps_at_n_distinct_doc_ids():
    chunks = [_chunk("d1", 0.2), _chunk("d1", 0.19), _chunk("d2", 0.18),
              _chunk("d3", 0.17), _chunk("d4", 0.16)]
    suggestions = refusal.suggestion_citations(chunks, n=3)
    assert len(suggestions) == 3
    assert len({s.doc_id for s in suggestions}) == 3
    assert [s.doc_id for s in suggestions] == ["d1", "d2", "d3"]
    assert all(isinstance(s, Citation) and s.quote for s in suggestions)


def test_post_llm_gate_fires_on_zero_citations():
    assert refusal.post_llm_gate([]) is True


def test_post_llm_gate_does_not_fire_with_at_least_one_citation():
    citation = Citation(chunk_id="d:0", doc_id="d", title="T", url="http://x", quote="q")
    assert refusal.post_llm_gate([citation]) is False
