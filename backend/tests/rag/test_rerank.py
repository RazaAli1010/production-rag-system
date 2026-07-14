"""F6 cross-encoder reranking unit + acceptance tests (requirements.md §4).

No live model: `sentence_transformers.CrossEncoder` never loads here. The cross-encoder is replaced
by a `FakeCrossEncoder` (subclassing `langchain_core.cross_encoders.BaseCrossEncoder`) whose score
returns caller-controlled logits, so the whole suite runs offline and fast. Covers the singleton
load (T2), batched offloaded scoring (T3), sigmoid calibration (T4), whole-object reorder + slice
(T5), the empty/degenerate guards (T5/AC-14/15), the LangChain API surface + off-path guard (T9),
the loop-lag probe (T11), and F5 toggle parity (T7).
"""

import asyncio
import time

import pytest
from langchain_core.cross_encoders import BaseCrossEncoder

from app.core.contracts import PipelineFlags, RetrievedChunk
from app.core.settings import Settings
from app.rag import rerank, retriever


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
    """Stand-in for HuggingFaceCrossEncoder: `.score` returns a logit per pair from `logit_of`
    (keyed by the pair's text), recording every call so batching + binding can be asserted."""

    def __init__(self, logit_of=None, block_s=0.0):
        self.logit_of = logit_of or {}
        self.block_s = block_s
        self.calls = []

    def score(self, text_pairs):
        self.calls.append(list(text_pairs))
        if self.block_s:
            time.sleep(self.block_s)  # simulate the blocking forward pass (loop-lag probe)
        return [self.logit_of.get(text, 0.0) for _q, text in text_pairs]


@pytest.fixture(autouse=True)
def _reset_model():
    rerank._RERANK_MODEL = None
    yield
    rerank._RERANK_MODEL = None


# ----------------------------------------------------------------- T2: singleton load (AC-1/AC-2)

async def test_get_rerank_model_loads_once_cpu_pinned_off_loop(monkeypatch):
    built = []

    class _Recorder:
        def __init__(self, model_name, model_kwargs):
            built.append((model_name, model_kwargs))

    monkeypatch.setattr(rerank, "HuggingFaceCrossEncoder", _Recorder)

    calls = {"n": 0}
    real_run_sync = rerank.anyio.to_thread.run_sync

    async def _spy(fn, *args):
        calls["n"] += 1
        return await real_run_sync(fn, *args)

    monkeypatch.setattr(rerank.anyio.to_thread, "run_sync", _spy)
    settings = _settings()

    first = await rerank.get_rerank_model(settings)
    second = await rerank.get_rerank_model(settings)

    assert second is first  # cached singleton (AC-1)
    assert calls["n"] == 1  # built once, through the thread offload (AC-2)
    assert built == [("cross-encoder/ms-marco-MiniLM-L-6-v2", {"device": "cpu"})]  # CPU pinned


async def test_get_rerank_model_concurrent_first_use_loads_once(monkeypatch):
    built = {"n": 0}

    def _slow_build(settings):
        built["n"] += 1
        time.sleep(0.05)
        return FakeCrossEncoder()

    monkeypatch.setattr(rerank, "_build_model", _slow_build)
    settings = _settings()

    await asyncio.gather(*(rerank.get_rerank_model(settings) for _ in range(5)))
    assert built["n"] == 1  # the lock serialized the one-time load (AC-2)


# --------------------------------------------------------- T3/T7: batched scoring + rerank (AC-6/7)

async def test_rerank_scores_in_one_batched_call(monkeypatch):
    model = FakeCrossEncoder()

    async def _get(_s):
        return model

    monkeypatch.setattr(rerank, "get_rerank_model", _get)
    pool = [_rc(f"c{i}", text=f"text {i}") for i in range(12)]

    await rerank.rerank_chunks("q", pool, _settings())

    assert len(model.calls) == 1  # ONE batched score call, not 12 (AC-7)
    assert len(model.calls[0]) == 12
    assert model.calls[0][0] == ("q", "text 0")  # (query, chunk_text) pairs


async def test_rerank_reorders_by_score_and_slices_top_n(monkeypatch):
    # RRF/pool order is c0..c4, but the cross-encoder judges c3 most relevant.
    model = FakeCrossEncoder(logit_of={"t0": 0.1, "t1": -2.0, "t2": 1.0, "t3": 5.0, "t4": 0.5})

    async def _get(_s):
        return model

    monkeypatch.setattr(rerank, "get_rerank_model", _get)
    pool = [_rc(f"c{i}", text=f"t{i}") for i in range(5)]

    out = await rerank.rerank_chunks("q", pool, _settings(RERANK_TOP_N=3))

    assert [c.chunk_id for c in out] == ["c3", "c2", "c4"]  # reranked, top-3 (AC-6/9)
    assert all(c.rerank_score is not None for c in out)
    assert out[0].rerank_score > out[1].rerank_score > out[2].rerank_score  # calibrated, monotonic


async def test_rerank_binds_score_and_metadata_to_whole_object(monkeypatch):
    # AC-9: scores + metadata must stay bound to their chunk through the reorder + slice — no
    # parallel-array re-zip drift. Each chunk carries a distinct page_start tied to its id.
    model = FakeCrossEncoder(logit_of={"low": -3.0, "high": 4.0})

    async def _get(_s):
        return model

    monkeypatch.setattr(rerank, "get_rerank_model", _get)
    pool = [_rc("A", text="low", page_start=11), _rc("B", text="high", page_start=22)]

    out = await rerank.rerank_chunks("q", pool, _settings(RERANK_TOP_N=5))

    assert [c.chunk_id for c in out] == ["B", "A"]  # B reranked above A
    by_id = {c.chunk_id: c for c in out}
    assert by_id["B"].page_start == 22 and by_id["A"].page_start == 11  # metadata travelled
    assert by_id["B"].rerank_score > by_id["A"].rerank_score


async def test_rerank_does_not_mutate_input_pool(monkeypatch):
    model = FakeCrossEncoder(logit_of={"x": 1.0})

    async def _get(_s):
        return model

    monkeypatch.setattr(rerank, "get_rerank_model", _get)
    pool = [_rc("c0", text="x")]

    await rerank.rerank_chunks("q", pool, _settings())
    assert pool[0].rerank_score is None  # model_copy: original untouched


# --------------------------------------------------------------- T4: calibration (AC-10/AC-11)

def test_calibrate_sigmoid_maps_to_unit_interval_and_preserves_order():
    settings = _settings(RERANK_APPLY_SIGMOID=True)
    out = rerank._calibrate([5.0, 0.0, -5.0], settings)
    assert all(0.0 < s < 1.0 for s in out)
    assert out[0] > out[1] > out[2]  # monotonic
    assert out[1] == pytest.approx(0.5)  # sigmoid(0) == 0.5
    assert out[0] > 0.99 and out[2] < 0.01  # large ± logits saturate


def test_calibrate_passthrough_when_sigmoid_disabled():
    settings = _settings(RERANK_APPLY_SIGMOID=False)
    assert rerank._calibrate([2.0, -1.0], settings) == [2.0, -1.0]


def test_sigmoid_stable_on_large_negative_logit():
    # Numerically stable both ways: a large negative logit must not overflow math.exp.
    assert rerank._sigmoid(-1000.0) == pytest.approx(0.0, abs=1e-9)
    assert rerank._sigmoid(1000.0) == pytest.approx(1.0, abs=1e-9)


# -------------------------------------------------------- T5: empty / degenerate guards (AC-14/15)

async def test_rerank_empty_pool_short_circuits_without_model_call(monkeypatch):
    async def _boom(_s):
        raise AssertionError("model must NOT be loaded for an empty pool (AC-14)")

    monkeypatch.setattr(rerank, "get_rerank_model", _boom)
    out = await rerank.rerank_chunks("q", [], _settings())
    assert out == []


def test_safe_text_falls_back_for_whitespace_only_chunk():
    assert rerank._safe_text(_rc("c", text="   ")) == "T"  # falls back to title (AC-15)
    assert rerank._safe_text(_rc("c", text="real body")) == "real body"
    chunk = RetrievedChunk(chunk_id="c", doc_id="d", title="", text="  ", section_heading="Sec 3")
    assert rerank._safe_text(chunk) == "Sec 3"  # prefers section heading over empty title


async def test_rerank_whitespace_chunk_does_not_break_batch(monkeypatch):
    model = FakeCrossEncoder(logit_of={"T": 0.2, "good body": 0.9})

    async def _get(_s):
        return model

    monkeypatch.setattr(rerank, "get_rerank_model", _get)
    pool = [_rc("blank", text="   "), _rc("ok", text="good body")]

    out = await rerank.rerank_chunks("q", pool, _settings(RERANK_TOP_N=5))
    assert {c.chunk_id for c in out} == {"blank", "ok"}  # both scored, batch intact (AC-15)
    assert out[0].chunk_id == "ok"


# --------------------------------------------------------------------- max_rerank_score (AC-10)

def test_max_rerank_score():
    assert rerank.max_rerank_score([_rc("a", rerank_score=0.3), _rc("b", rerank_score=0.8)]) == 0.8
    assert rerank.max_rerank_score([_rc("a")]) == 0.0  # none reranked → 0.0
    assert rerank.max_rerank_score([]) == 0.0


# ----------------------------------------------------- T7: seam integration (rerank on/off, AC-17)

async def test_retrieve_rerank_off_is_f5_passthrough(monkeypatch):
    # ENABLE_RERANK False (default) → byte-for-byte F5 pool[:k]; rerank must not be consulted.
    settings = _settings(ENABLE_HYBRID=True)
    pool = [_rc(f"c{i}", dense_score=1.0 - i / 100) for i in range(12)]

    async def _hybrid(query, k, namespace, s):
        return pool

    monkeypatch.setattr("app.rag.hybrid.hybrid_retrieve", _hybrid)
    monkeypatch.setattr(rerank, "rerank_chunks",
                        lambda *a, **k: pytest.fail("rerank used while ENABLE_RERANK off"))

    out = await retriever.retrieve("q", k=5, namespace=None, settings=settings)
    assert [c.chunk_id for c in out] == [f"c{i}" for i in range(5)]  # F5 top-k (AC-17)


async def test_retrieve_rerank_on_reranks_pool_to_top_n(monkeypatch):
    settings = _settings(ENABLE_HYBRID=True, ENABLE_RERANK=True, RERANK_TOP_N=5)
    # 12-candidate fused pool; the cross-encoder promotes c11 (last in RRF order) to the top.
    pool = [_rc(f"c{i}", text=f"t{i}", dense_score=1.0 - i / 100) for i in range(12)]
    logit_of = {f"t{i}": float(i) for i in range(12)}  # higher index → higher relevance
    model = FakeCrossEncoder(logit_of=logit_of)

    async def _hybrid(query, k, namespace, s):
        assert k == 5  # hybrid_retrieve still receives the generation k (returns the ≤12 pool)
        return pool

    async def _get(_s):
        return model

    monkeypatch.setattr("app.rag.hybrid.hybrid_retrieve", _hybrid)
    monkeypatch.setattr(rerank, "get_rerank_model", _get)

    out = await retriever.retrieve("q", k=5, namespace=None, settings=settings)

    assert [c.chunk_id for c in out] == ["c11", "c10", "c9", "c8", "c7"]  # reranked top-5 (AC-6)
    assert all(c.rerank_score is not None for c in out)


async def test_retrieve_rerank_on_dense_only_widens_pool(monkeypatch):
    # Diagnostic dense_only + rerank: the pool is widened to RERANK_CANDIDATE_K so the cross-encoder
    # has more than k to re-order (design §4).
    settings = _settings(ENABLE_RERANK=True, RERANK_CANDIDATE_K=12, RERANK_TOP_N=5)
    seen_k = {}

    async def _dense(query, k, namespace, s):
        seen_k["k"] = k
        return [_rc(f"c{i}", text=f"t{i}") for i in range(k)]

    async def _get(_s):
        return FakeCrossEncoder(logit_of={f"t{i}": float(i) for i in range(12)})

    monkeypatch.setattr(retriever, "dense_retrieve", _dense)
    monkeypatch.setattr(rerank, "get_rerank_model", _get)

    out = await retriever.retrieve("q", k=5, namespace="pu", settings=settings)
    assert seen_k["k"] == 12  # widened pool (AC-6)
    assert len(out) == 5


# --------------------------------------------------- T8: flag overlay maps rerank (AC-18)

def test_apply_flags_maps_rerank_and_does_not_mutate():
    from app.rag import flags as flags_mod

    settings = _settings()
    overlaid = flags_mod.apply_flags(settings, PipelineFlags(hybrid=True, rerank=True))
    assert overlaid.ENABLE_RERANK is True and overlaid.ENABLE_HYBRID is True
    assert settings.ENABLE_RERANK is False  # original untouched (copy, not in-place)


# ------------------------------------------------------ T9: LangChain API surface + off-path (AC-3)

async def test_build_compression_retriever_uses_shared_model(monkeypatch):
    model = FakeCrossEncoder()

    async def _get(_s):
        return model

    monkeypatch.setattr(rerank, "get_rerank_model", _get)

    ccr = await rerank.build_compression_retriever(_settings(RERANK_TOP_N=5))
    assert ccr.base_compressor.model is model  # SAME shared instance (AC-3), zero extra memory
    assert ccr.base_compressor.top_n == 5


async def test_compression_retriever_returns_top_n(monkeypatch):
    # The LangChain surface actually reranks + slices to top_n when exercised via its async API.
    model = FakeCrossEncoder(logit_of={f"t{i}": float(i) for i in range(8)})

    async def _get(_s):
        return model

    async def _hybrid(query, k, namespace, s):
        return [_rc(f"c{i}", text=f"t{i}") for i in range(8)]

    monkeypatch.setattr(rerank, "get_rerank_model", _get)
    monkeypatch.setattr("app.rag.hybrid.hybrid_retrieve", _hybrid)

    ccr = await rerank.build_compression_retriever(_settings(RERANK_TOP_N=3))
    docs = await ccr.ainvoke("q")
    assert len(docs) == 3  # top_n after cross-encoder rerank


async def test_compression_retriever_never_on_request_path(monkeypatch):
    # AC-3: generation reranks via the direct path (rerank_chunks); the compression retriever (which
    # re-retrieves) is NEVER invoked. Guard: build_compression_retriever must not be called during a
    # retrieve() with rerank on.
    settings = _settings(ENABLE_HYBRID=True, ENABLE_RERANK=True)

    async def _hybrid(query, k, namespace, s):
        return [_rc("c0", text="t")]

    async def _get(_s):
        return FakeCrossEncoder(logit_of={"t": 1.0})

    monkeypatch.setattr("app.rag.hybrid.hybrid_retrieve", _hybrid)
    monkeypatch.setattr(rerank, "get_rerank_model", _get)
    monkeypatch.setattr(rerank, "build_compression_retriever",
                        lambda *a, **k: pytest.fail("compression retriever used on request path"))

    out = await retriever.retrieve("q", k=5, namespace=None, settings=settings)
    assert out and out[0].chunk_id == "c0"


# ------------------------------------------------------ T11: loop-lag probe + latency (AC-5/8)

async def test_event_loop_stays_responsive_during_rerank(monkeypatch):
    # The forward pass blocks its worker thread for 150ms; because rerank offloads via
    # anyio.to_thread.run_sync, the event loop keeps ticking (AC-5). If scoring ran inline, a tick
    # would stall the full 150ms.
    model = FakeCrossEncoder(logit_of={}, block_s=0.15)

    async def _get(_s):
        return model

    monkeypatch.setattr(rerank, "get_rerank_model", _get)
    pool = [_rc(f"c{i}", text=f"t{i}") for i in range(12)]

    lags = []

    async def ticker():
        last = time.perf_counter()
        try:
            while True:
                await asyncio.sleep(0.005)
                now = time.perf_counter()
                lags.append(now - last)
                last = now
        except asyncio.CancelledError:
            return

    t = asyncio.create_task(ticker())
    await rerank.rerank_chunks("q", pool, _settings())
    t.cancel()

    assert lags, "ticker never ran"
    assert max(lags) < 0.10  # loop kept ticking; well under the 150ms blocking score


async def test_rerank_records_rerank_ms(monkeypatch):
    logged = {}

    def _log(rerank_ms, max_score, n_candidates):
        logged.update(rerank_ms=rerank_ms, max_score=max_score, n_candidates=n_candidates)

    async def _get(_s):
        return FakeCrossEncoder(logit_of={"t0": 2.0, "t1": 1.0})

    monkeypatch.setattr(rerank.observability, "log_rerank", _log)
    monkeypatch.setattr(rerank, "get_rerank_model", _get)
    pool = [_rc("c0", text="t0"), _rc("c1", text="t1")]

    await rerank.rerank_chunks("q", pool, _settings())
    assert isinstance(logged["rerank_ms"], int) and logged["rerank_ms"] >= 0  # AC-8/AC-20
    assert logged["n_candidates"] == 2
    assert logged["max_score"] == pytest.approx(rerank._sigmoid(2.0))
