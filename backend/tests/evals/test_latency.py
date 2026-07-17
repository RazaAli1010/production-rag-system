"""T8 — latency suite (percentiles + per-stage ms + tokens/cost)."""

from app.core.contracts import PipelineFlags
from app.evals import latency as L
from app.evals.schemas import EvalRecord
from app.rag.events import SSEEvent, stage_event
from tests.evals.conftest import fake_sessionmaker, make_settings


def test_percentiles():
    p = L._percentiles([float(i) for i in range(1, 101)])  # 1..100
    assert p["p50"] == 50.0
    assert p["p95"] == 95.0
    assert p["p99"] == 99.0


def test_percentiles_single_value():
    assert L._percentiles([7.0]) == {"p50": 7.0, "p95": 7.0, "p99": 7.0}


async def test_run_latency_reads_stage_ms_and_counts_tokens():
    async def fake_astream(q, k, ns, flags, *, session, settings, sessionmaker=None):
        yield stage_event("searching", "started")
        yield stage_event("searching", "done", ms=10)
        yield stage_event("generating", "started")
        yield SSEEvent(event="token", data={"token": "a"})
        yield SSEEvent(event="token", data={"token": "b"})
        yield stage_event("generating", "done", ms=40)
        yield SSEEvent(event="citations", data={"citations": []})
        yield SSEEvent(event="meta", data={})
        yield SSEEvent(event="done", data={})

    recs = [EvalRecord(qid="q", question="?", ground_truth_answer="g",
                       source_doc_ids=["d1"], source_pages_or_anchors=["1"], tags=["en"])]
    s = make_settings(EVAL_LATENCY_REQUESTS=3)
    res = await L.run_latency(recs, PipelineFlags(), s,
                              sessionmaker=fake_sessionmaker, astream=fake_astream)
    m = {x.metric: x.value for x in res.metrics}
    assert m["latency_searching_p50"] == 10.0
    assert m["latency_generating_p95"] == 40.0
    assert m["tokens_mean"] == 2.0        # two token events per request
    assert "latency_p50" in m and "cost_mean" in m


async def test_run_latency_empty_when_only_ooc():
    recs = [EvalRecord(qid="o", question="?", ground_truth_answer="", tags=["out_of_corpus"])]
    res = await L.run_latency(recs, PipelineFlags(), make_settings(),
                              sessionmaker=fake_sessionmaker, astream=None)
    assert res.metrics == []
