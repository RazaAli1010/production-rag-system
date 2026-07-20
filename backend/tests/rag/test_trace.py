"""Pipeline trace — each stage's intermediate output reaches the `detail` field of its `done` frame.

This is what makes the pipeline demonstrable rather than inferred from timings, so the checks are
about the data surviving the trip out: recorded at the seam, drained exactly once by `stages.emit`,
absent when `ENABLE_TRACE` is off, and — the one that is easy to get wrong — visible to the parent
when the seam ran inside an `asyncio.gather` child task.
"""

import asyncio

import pytest
from langchain_core.cross_encoders import BaseCrossEncoder

from app.core.contracts import RetrievedChunk
from app.core.settings import Settings
from app.memory import stages
from app.rag import rerank, trace


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


def _rc(chunk_id, text="body text here", **o):
    return RetrievedChunk(chunk_id=chunk_id, doc_id="d", title="T", text=text, **o)


class FakeCrossEncoder(BaseCrossEncoder):
    def __init__(self, logits):
        self.logits = logits

    def score(self, pairs):
        return [self.logits[text] for _q, text in pairs]


def test_detail_rides_the_done_frame_only():
    trace.start(_settings())
    trace.record("searching", {"n": 1})

    assert stages.emit("searching", "started").data.get("detail") is None
    assert stages.emit("searching", "done", ms=5).data.get("detail") == {"n": 1}


def test_detail_is_drained_so_it_cannot_reattach():
    """`pop`, not `get`: `searching` closes once per request, and a leftover entry would surface on
    an unrelated later stage of the same name."""
    trace.start(_settings())
    trace.record("searching", {"n": 1})

    assert stages.emit("searching", "done", ms=5).data.get("detail") == {"n": 1}
    assert stages.emit("searching", "done", ms=5).data.get("detail") is None


def test_trace_off_records_nothing():
    trace.start(_settings(ENABLE_TRACE=False))
    trace.record("searching", {"n": 1})
    assert stages.emit("searching", "done", ms=5).data.get("detail") is None


async def test_record_from_a_gather_child_reaches_the_parent():
    """The reason the ContextVar holds a mutable dict. With query rewrite on, `hybrid_retrieve` runs
    inside `asyncio.gather` children (`rewrite.multi_query_retrieve`); each child gets a COPY of the
    context, so a `.set()` in there would be invisible out here and every fan-out card would vanish.
    """
    trace.start(_settings())

    async def child(i):
        trace.append("searching", {"query": f"q{i}"})

    await asyncio.gather(*(child(i) for i in range(3)))

    detail = stages.emit("searching", "done", ms=5).data.get("detail")
    assert {r["query"] for r in detail["runs"]} == {"q0", "q1", "q2"}


async def test_rerank_records_its_reordering(monkeypatch):
    """The demo's whole point for this stage: the trace must show the cross-encoder MOVING things,
    so a passage promoted from last to first is visible as such."""
    trace.start(_settings())
    pool = [_rc("d:1", "worst"), _rc("d:2", "middle"), _rc("d:3", "best")]
    fake = FakeCrossEncoder({"worst": -4.0, "middle": 0.0, "best": 6.0})
    monkeypatch.setattr(rerank, "get_rerank_model", lambda _s: _ready(fake))

    top = await rerank.rerank_chunks("q", pool, _settings(RERANK_TOP_N=3))

    detail = stages.emit("reranking", "done", ms=5).data.get("detail")
    assert [c.chunk_id for c in top] == ["d:3", "d:2", "d:1"]
    assert [r["chunk_id"] for r in detail["before"]] == ["d:1", "d:2", "d:3"]
    assert [r["chunk_id"] for r in detail["after"]] == ["d:3", "d:2", "d:1"]
    assert detail["after"][0]["moved"] == 2  # last of three → first
    assert detail["after"][0]["score"] > detail["after"][-1]["score"]


def test_payloads_are_capped():
    """These ride the same SSE stream as live tokens, so a 40-chunk pool must not become a 40-chunk
    frame and a long passage must not ship whole."""
    rows = trace.chunk_rows([_rc(f"d:{i}", "x" * 5_000) for i in range(40)])
    assert len(rows) == trace.MAX_ITEMS
    assert len(rows[0]["text"]) <= trace.MAX_TEXT + 1  # +1 for the ellipsis


async def _ready(value):
    return value


@pytest.fixture(autouse=True)
def _isolate_trace():
    """Each test starts with no trace installed, so a leak between tests can't fake a pass."""
    yield
    trace.start(_settings(ENABLE_TRACE=False))
