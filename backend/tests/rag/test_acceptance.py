"""Feature-level definition of done (requirements.md §4) — the five acceptance criteria that
gate F3. Items 3 (zero-citation conversion), 4 (prompt-injection guard placement), and 5
(answer()/astream() agreement) are exercised in dedicated files (`test_streaming.py`,
`test_prompt_injection.py`) — referenced here rather than duplicated, per tasks.md T17's
"pytest tests/rag/ green including all five acceptance tests" (the suite, not one file, is the
unit of "done"). This file owns items 1 and 2, the two that need the committed fixtures.
"""

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from sqlalchemy import select

from app.core.contracts import RetrievedChunk
from app.core.settings import Settings
from app.db.models.corpus import Chunk as ChunkRow
from app.db.models.corpus import Document as DocRow
from app.rag import baseline, retriever


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


async def _chunks_for_doc(session, doc_id):
    stmt = (
        select(ChunkRow, DocRow)
        .join(DocRow, ChunkRow.doc_id == DocRow.doc_id)
        .where(ChunkRow.doc_id == doc_id)
        .order_by(ChunkRow.seq)
    )
    result = await session.execute(stmt)
    return [
        RetrievedChunk(chunk_id=c.chunk_id, doc_id=d.doc_id, title=d.title, text=c.text,
                       section_heading=c.section_heading, page_start=c.page_start,
                       page_end=c.page_end, anchor=c.anchor, dense_score=0.9)
        for c, d in result.all()
    ]


# --- AC item 1: 10 smoke questions stream complete answers with >=1 valid citation each -------

async def test_smoke_questions_stream_complete_answers_with_at_least_one_citation(
    monkeypatch, seeded_corpus, smoke_questions
):
    session = seeded_corpus
    non_probe_questions = [q for q in smoke_questions if not q["out_of_corpus"]]
    assert len(non_probe_questions) == 10

    for q in non_probe_questions:
        expected_doc_id = q["expected_doc_ids"][0]
        chunks = await _chunks_for_doc(session, expected_doc_id)
        assert chunks, f"fixture doc {expected_doc_id} has no chunks"

        # `query_vec` must precede the `_chunks` capture: it is the 5th POSITIONAL arg of the real
        # seam, so putting it after would bind the caller's query_vec to `_chunks`.
        async def _fake_retrieve(query, k, namespace, settings, query_vec=None, _chunks=chunks):
            return _chunks

        monkeypatch.setattr(retriever, "retrieve", _fake_retrieve)
        monkeypatch.setattr(
            baseline, "build_llm",
            lambda settings: GenericFakeChatModel(
                messages=iter([AIMessage(content="Per the policy [1], this is the answer.")])
            ),
        )

        events = [ev async for ev in baseline.astream(q["question"], session=session,
                                                       settings=_settings())]

        assert not any(ev.event == "error" for ev in events), (q["question"], events)
        assert any(ev.event == "token" for ev in events), f"no tokens streamed for: {q['question']}"
        meta = next(e for e in events if e.event == "meta").data
        assert meta["refused"] is False
        assert len(meta["citations"]) >= 1
        assert meta["citations"][0]["doc_id"] == expected_doc_id


# --- AC item 2: out-of-corpus probe triggers pre-LLM refusal with <=3 suggestions -------------

async def test_out_of_corpus_probe_triggers_pre_llm_refusal(
    monkeypatch, seeded_corpus, smoke_questions
):
    session = seeded_corpus
    probe = next(q for q in smoke_questions if q["out_of_corpus"])
    assert probe["question"]

    # A real out-of-corpus question would score below REFUSAL_DENSE_THRESHOLD against every
    # namespace — simulate that low-confidence retrieval directly (F3's retriever seam is F4's
    # job to validate against a real embedding index, not F3's).
    low_score_chunks = await _chunks_for_doc(session, "pu-academic-probation-2023")
    for c in low_score_chunks:
        c.dense_score = 0.01

    async def _fake_low_score_retrieve(query, k, namespace, settings, query_vec=None):
        return low_score_chunks

    monkeypatch.setattr(retriever, "retrieve", _fake_low_score_retrieve)
    llm_constructed = []
    monkeypatch.setattr(baseline, "build_llm", lambda settings: llm_constructed.append(1))

    events = [ev async for ev in baseline.astream(probe["question"], session=session,
                                                   settings=_settings())]

    assert llm_constructed == []  # LLM never invoked (AC-6)
    meta = next(e for e in events if e.event == "meta").data
    assert meta["refused"] is True
    assert meta["refusal_reason"] == "low_retrieval_confidence"
    assert len(meta["citations"]) <= _settings().REFUSAL_SUGGESTION_COUNT
    assert not any(ev.event == "token" for ev in events)


# --- AC items 3/4/5: covered in dedicated files, referenced here for the DoD checklist ---------
# item 3 (zero-citation -> no_grounded_claims):      tests/rag/test_streaming.py
#   ::test_zero_citation_answer_converts_to_refusal
# item 4 (prompt-injection guard placement):         tests/rag/test_prompt_injection.py
#   ::test_prompt_injection_guard_precedes_injected_chunk_text
# item 5 (answer()/astream() terminal-field agreement): tests/rag/test_streaming.py
#   ::test_astream_and_answer_agree_on_terminal_fields
