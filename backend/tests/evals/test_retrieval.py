"""T4 — retrieval suite (hit@k, MRR, ooc exclusion, seam spy)."""

from app.core.contracts import RetrievedChunk
from app.evals import retrieval as R
from app.evals.schemas import EvalRecord
from tests.evals.conftest import make_settings


def _chunk(doc, page=None, anchor=None):
    return RetrievedChunk(chunk_id=f"{doc}:0", doc_id=doc, title="t", text="x",
                          page_start=page, page_end=page, anchor=anchor)


def _rec(tags=("en",), doc="d1", pages=("12",)):
    return EvalRecord(qid="q", question="?", ground_truth_answer="g",
                      source_doc_ids=[doc], source_pages_or_anchors=list(pages), tags=list(tags))


def test_is_hit_doc_and_page():
    rec = _rec()
    assert R._is_hit(_chunk("d1", 12), rec) is True
    assert R._is_hit(_chunk("d1", 99), rec) is False   # page miss
    assert R._is_hit(_chunk("d2", 12), rec) is False   # wrong doc


def test_is_hit_page_range_overlap():
    rec = _rec(pages=("13",))
    c = RetrievedChunk(chunk_id="d1:0", doc_id="d1", title="t", text="x",
                       page_start=12, page_end=14)
    assert R._is_hit(c, rec) is True


def test_is_hit_anchor():
    rec = _rec(pages=("clause-7",))
    assert R._is_hit(_chunk("d1", anchor="clause-7"), rec) is True


def test_is_hit_doc_level_when_no_pages():
    rec = _rec(pages=())
    assert R._is_hit(_chunk("d1"), rec) is True


def test_hit_at_k_and_rr():
    rec = _rec()
    ranked = [_chunk("dX", 1), _chunk("d1", 12), _chunk("dY", 3)]
    assert R._hit_at_k(ranked, rec, 1) == 0.0
    assert R._hit_at_k(ranked, rec, 3) == 1.0
    assert R._reciprocal_rank(ranked, rec) == 0.5


async def test_run_retrieval_calls_seam_and_excludes_ooc():
    calls = []

    async def spy_retrieve(q, k, ns, s):
        calls.append((q, k, ns))
        return [_chunk("dX", 1), _chunk("d1", 12)]

    recs = [_rec(tags=("en",)),
            _rec(tags=("code_switched",)),
            EvalRecord(qid="o", question="?", ground_truth_answer="g", tags=["out_of_corpus"])]
    s = make_settings()
    res = await R.run_retrieval(recs, None, s, retrieve=spy_retrieve)

    assert len(calls) == 2  # ooc excluded
    assert all(ns is None for _, _, ns in calls)  # namespace fan-out
    metrics = {(m.metric, m.slice_tag): m.value for m in res.metrics}
    assert metrics[("hit@3", None)] == 1.0
    assert metrics[("hit@1", None)] == 0.0
    assert ("mrr", "en") in metrics and ("mrr", "code_switched") in metrics
