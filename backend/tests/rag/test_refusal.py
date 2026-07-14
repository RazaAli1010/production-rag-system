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


# --- F6: calibrated rerank gate swap (AC-12) ---

def _reranked(doc_id, rerank_score, dense_score=None):
    return RetrievedChunk(chunk_id=f"{doc_id}:0", doc_id=doc_id, title=f"Title {doc_id}",
                          text="chunk body", dense_score=dense_score, rerank_score=rerank_score)


def test_pre_llm_gate_uses_rerank_score_when_rerank_enabled():
    settings = _settings(ENABLE_RERANK=True, REFUSAL_RERANK_THRESHOLD=0.5,
                         REFUSAL_DENSE_THRESHOLD=0.25)
    # dense_score is low (would refuse under the F5 gate) but the calibrated rerank score is high →
    # the rerank gate, which actually read query↔chunk, does NOT refuse (AC-12).
    chunks = [_reranked("d1", rerank_score=0.8, dense_score=0.1)]
    assert refusal.pre_llm_gate(chunks, settings) is False


def test_pre_llm_gate_refuses_when_max_rerank_below_threshold():
    settings = _settings(ENABLE_RERANK=True, REFUSAL_RERANK_THRESHOLD=0.5)
    chunks = [_reranked("d1", rerank_score=0.4), _reranked("d2", rerank_score=0.2)]
    assert refusal.pre_llm_gate(chunks, settings) is True  # every rerank score below threshold


def test_pre_llm_gate_rerank_off_still_uses_dense_gate():
    # ENABLE_RERANK False (default) → unchanged F5 dense-cosine behaviour, even if rerank_score set.
    settings = _settings(REFUSAL_DENSE_THRESHOLD=0.25)
    chunks = [_reranked("d1", rerank_score=0.99, dense_score=0.1)]  # high rerank, weak dense
    assert refusal.pre_llm_gate(chunks, settings) is True  # dense gate refuses; rerank ignored


def test_pre_llm_gate_rerank_enabled_empty_still_refuses():
    settings = _settings(ENABLE_RERANK=True, REFUSAL_RERANK_THRESHOLD=0.5)
    assert refusal.pre_llm_gate([], settings) is True


def test_post_llm_gate_fires_on_zero_citations():
    assert refusal.post_llm_gate([]) is True


def test_post_llm_gate_does_not_fire_with_at_least_one_citation():
    citation = Citation(chunk_id="d:0", doc_id="d", title="T", url="http://x", quote="q")
    assert refusal.post_llm_gate([citation]) is False
