"""T7 — refusal suite (recall, false-refusal, reason breakdown)."""

from app.core.contracts import AnswerResponse, PipelineFlags
from app.evals import refusal as RF
from app.evals.schemas import EvalRecord
from tests.evals.conftest import fake_sessionmaker, make_settings


def _answerable(qid):
    return EvalRecord(qid=qid, question="?", ground_truth_answer="g",
                      source_doc_ids=["d1"], source_pages_or_anchors=["1"], tags=["en"])


def _ooc(qid):
    return EvalRecord(qid=qid, question="?", ground_truth_answer="", tags=["out_of_corpus"])


async def test_refusal_metrics(monkeypatch):
    # ooc-1 refused (good), ooc-2 NOT refused (miss) -> recall 0.5
    # ans-1 wrongly refused (no_grounded_claims), ans-2 answered -> false-refusal 0.5
    plan = {
        "ooc-1": (True, "low_retrieval_confidence"),
        "ooc-2": (False, None),
        "ans-1": (True, "no_grounded_claims"),
        "ans-2": (False, None),
    }

    async def fake_answer(q, k, ns, flags, *, session, settings):
        # encode the qid in the question so we can look up the planned outcome
        refused, reason = plan[q]
        return AnswerResponse(answer="", refused=refused, refusal_reason=reason,
                              pipeline_flags=flags)

    recs = [_ooc("ooc-1"), _ooc("ooc-2"), _answerable("ans-1"), _answerable("ans-2")]
    for r in recs:
        r.question = r.qid  # so fake_answer can map

    res = await RF.run_refusal(recs, PipelineFlags(), make_settings(),
                               sessionmaker=fake_sessionmaker, answer=fake_answer)
    m = {x.metric: x.value for x in res.metrics}
    assert m["refusal_recall"] == 0.5
    assert m["false_refusal_rate"] == 0.5
    assert m["refusals_low_retrieval_confidence"] == 1.0
    assert m["refusals_no_grounded_claims"] == 1.0
