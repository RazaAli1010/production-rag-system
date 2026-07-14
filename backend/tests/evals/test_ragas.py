"""T5-6 — RAGAS suite: cost preview, confirm-gate no-spend, thread offload."""

import anyio

from app.core.contracts import AnswerResponse, PipelineFlags, RetrievedChunk
from app.evals import ragas_suite as RS
from app.evals.schemas import EvalRecord
from tests.evals.conftest import fake_sessionmaker, make_settings


def _recs():
    return [
        EvalRecord(qid="q1", question="how to apply", ground_truth_answer="by form X",
                   source_doc_ids=["d1"], source_pages_or_anchors=["1"], tags=["en"]),
        EvalRecord(qid="o", question="?", ground_truth_answer="", tags=["out_of_corpus"]),
    ]


def test_preview_judge_cost_positive():
    tokens, usd = RS.preview_judge_cost(_recs(), make_settings())
    assert tokens > 0 and usd > 0


async def test_confirm_false_spends_nothing():
    called = {"answer": 0, "retrieve": 0}

    async def fake_answer(*a, **k):
        called["answer"] += 1

    async def fake_retrieve(*a, **k):
        called["retrieve"] += 1
        return []

    res = await RS.run_ragas(_recs(), PipelineFlags(), make_settings(), confirm=False,
                             sessionmaker=fake_sessionmaker, answer=fake_answer,
                             retrieve=fake_retrieve)
    assert res.metrics == []
    assert called == {"answer": 0, "retrieve": 0}


async def test_confirm_true_offloads_and_emits_four_metrics(monkeypatch):
    async def fake_answer(q, k, ns, flags, *, session, settings):
        return AnswerResponse(answer="ans", pipeline_flags=flags)

    async def fake_retrieve(q, k, ns, s):
        return [RetrievedChunk(chunk_id="c", doc_id="d1", title="t", text="ctx")]

    monkeypatch.setattr(RS, "_build_judge", lambda s: (None, None))
    monkeypatch.setattr(RS, "_evaluate_sync", lambda samples, llm, emb: (
        {"faithfulness": 0.9, "answer_relevancy": 0.8,
         "context_precision": 0.7, "context_recall": 0.6}, 123))

    offloaded = {"n": 0}
    real_run_sync = anyio.to_thread.run_sync

    async def spy_run_sync(func, *a, **k):
        offloaded["n"] += 1
        return await real_run_sync(func, *a, **k)

    monkeypatch.setattr(anyio.to_thread, "run_sync", spy_run_sync)

    res = await RS.run_ragas(_recs(), PipelineFlags(), make_settings(), confirm=True,
                             sessionmaker=fake_sessionmaker, answer=fake_answer,
                             retrieve=fake_retrieve)
    assert offloaded["n"] == 1  # AC-12: evaluate() ran via anyio.to_thread.run_sync
    names = {m.metric for m in res.metrics}
    assert names == {"faithfulness", "answer_relevancy", "context_precision", "context_recall"}
