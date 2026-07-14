"""F5 hybrid-search unit + acceptance tests (requirements.md §4).

No live services: BM25 is a synthetic in-memory `BM25Okapi`, dense retrieval and `index.fetch` are
injected fakes (F2/F3 dependency-injection style, not a mock lib). Covers RRF math + dedupe (T5),
degraded mode (T9), the fusion-safe refusal interaction (T10), Urdu tokenizer parity (T3), and
baseline toggle parity (T7).
"""

import pytest
from rank_bm25 import BM25Okapi

from app.core.contracts import PipelineFlags, RetrievedChunk
from app.core.settings import Settings
from app.indexing.bm25 import urdu_safe_tokenize
from app.rag import flags as flags_mod
from app.rag import hybrid, refusal, retriever


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


def _rc(chunk_id, doc_id="d", dense_score=None):
    return RetrievedChunk(chunk_id=chunk_id, doc_id=doc_id, title="T", text="body",
                          dense_score=dense_score)


@pytest.fixture(autouse=True)
def _reset_bm25_cache():
    hybrid._BM25_CACHE = None
    hybrid.was_degraded()  # clear any leaked degraded flag
    yield
    hybrid._BM25_CACHE = None


# ----------------------------------------------------------------- T3: sparse scoring + Urdu parity

# BM25Okapi's IDF is ≤ 0 for a term present in ≥ half the corpus (common-term penalty). The real
# ~600-chunk index makes query terms rare (positive IDF); synthetic tests must pad with distractors
# so the matched term stays rare, otherwise `sparse_scores`' correct `score > 0` filter drops it.
_DISTRACTORS = [
    "hostel allotment and mess charges for resident scholars",
    "convocation gown return deadline notification",
    "library fine waiver committee minutes",
    "sports gala volunteer registration form",
    "transport route timings for city campus shuttle",
]


def _bm25_cache(corpus_texts, chunk_ids, pad=True):
    texts = list(corpus_texts)
    ids = list(chunk_ids)
    if pad:
        for i, d in enumerate(_DISTRACTORS):
            texts.append(d)
            ids.append(f"pad{i}:0")
    corpus = [urdu_safe_tokenize(t) for t in texts]
    return {"bm25": BM25Okapi(corpus), "chunk_ids": ids}


def test_sparse_scores_ranks_exact_term_first():
    cache = _bm25_cache(
        ["fee refund policy for undergraduates", "probation removal rules", "hostel allotment"],
        ["a:0", "b:0", "c:0"],
    )
    ranked = hybrid.sparse_scores("fee refund", cache, top_k=20)
    assert ranked[0][0] == "a:0"
    assert [r for _, _, r in ranked] == list(range(1, len(ranked) + 1))  # contiguous ranks from 1
    assert all(score > 0 for _, score, _ in ranked)  # zero-overlap chunks dropped


def test_sparse_scores_preserves_urdu_tokens():
    # Urdu tokenizer parity (US-7/AC-3): the query tokenizes with the exact corpus tokenizer.
    assert urdu_safe_tokenize("پروبیشن fee") == ["پروبیشن", "fee"]
    cache = _bm25_cache(["پروبیشن سے نکلنے کا طریقہ", "fee schedule"], ["u:0", "f:0"])
    ranked = hybrid.sparse_scores("پروبیشن", cache, top_k=20)
    assert ranked and ranked[0][0] == "u:0"


def test_sparse_scores_empty_corpus_returns_empty():
    assert hybrid.sparse_scores("q", {"bm25": None, "chunk_ids": []}, top_k=20) == []


# ------------------------------------------------------------------- T4: fetch hydration

class _FakeVector:
    def __init__(self, metadata):
        self.metadata = metadata


class _FakeFetchResponse:
    def __init__(self, vectors):
        self.vectors = vectors


class _FakeIndex:
    """Mirrors `IndexAsyncio.fetch(ids, namespace) -> FetchResponse` — returns only the ids that
    live in the requested namespace (how Pinecone filters the global BM25 candidates, design §5)."""

    def __init__(self, by_namespace):
        self.by_namespace = by_namespace
        self.calls = []

    async def fetch(self, ids, namespace=None):
        self.calls.append((tuple(ids), namespace))
        present = self.by_namespace.get(namespace, {})
        vectors = {cid: _FakeVector(present[cid]) for cid in ids if cid in present}
        return _FakeFetchResponse(vectors)


def _md(doc_id, text="chunk text", **o):
    return {"doc_id": doc_id, "title": "T", "text": text, "section_heading": "",
            "page_start": -1, "page_end": -1, "anchor": "", "token_count": 3, **o}


async def test_hydrate_sparse_only_builds_chunks_from_metadata(monkeypatch):
    index = _FakeIndex({"pu": {"a:0": _md("pu-doc", page_start=5, page_end=5)}})
    monkeypatch.setattr(hybrid, "get_index", lambda s: index)

    hydrated = await hybrid.hydrate_sparse_only(["a:0"], "pu", _settings())
    assert set(hydrated) == {"a:0"}
    chunk = hydrated["a:0"]
    assert chunk.doc_id == "pu-doc" and chunk.page_start == 5
    assert chunk.section_heading is None and chunk.anchor is None  # -1/"" sentinels normalized


async def test_hydrate_sparse_only_drops_ids_absent_from_namespace(monkeypatch):
    index = _FakeIndex({"pu": {"a:0": _md("pu-doc")}})  # "b:0" lives nowhere in pu
    monkeypatch.setattr(hybrid, "get_index", lambda s: index)

    hydrated = await hybrid.hydrate_sparse_only(["a:0", "b:0"], "pu", _settings())
    assert set(hydrated) == {"a:0"}  # absent id dropped, not raised (AC-4)


async def test_hydrate_sparse_only_merges_across_namespaces_when_none(monkeypatch):
    index = _FakeIndex({"pu": {"a:0": _md("pu-doc")}, "hec": {"b:0": _md("hec-doc")}})
    monkeypatch.setattr(hybrid, "get_index", lambda s: index)
    settings = _settings(RETRIEVAL_NAMESPACES=["pu", "hec"])

    hydrated = await hybrid.hydrate_sparse_only(["a:0", "b:0"], None, settings)
    assert set(hydrated) == {"a:0", "b:0"}  # fetched from both namespaces and merged
    assert {ns for _, ns in index.calls} == {"pu", "hec"}


async def test_hydrate_sparse_only_swallows_fetch_failure(monkeypatch):
    # In degraded mode hydration also hits Pinecone; a fetch failure must not crash the answer,
    # just yield fewer/no sparse candidates (design.md §6, hybrid robustness).
    class _FailingIndex:
        async def fetch(self, ids, namespace=None):
            raise RuntimeError("pinecone unreachable")

    monkeypatch.setattr(hybrid, "get_index", lambda s: _FailingIndex())
    hydrated = await hybrid.hydrate_sparse_only(["a:0"], "pu", _settings())
    assert hydrated == {}  # swallowed, no raise


async def test_hydrate_sparse_only_empty_ids_makes_no_fetch(monkeypatch):
    index = _FakeIndex({})
    monkeypatch.setattr(hybrid, "get_index", lambda s: index)
    assert await hybrid.hydrate_sparse_only([], None, _settings()) == {}
    assert index.calls == []  # already-dense ids never re-fetched


# --------------------------------------------------------------------------- T5: RRF math + dedupe

def test_rrf_fuse_math_on_synthetic_lists():
    settings = _settings(HYBRID_RRF_K=60, HYBRID_FUSED_TOP_K=12)
    dense = [_rc("a", dense_score=0.9), _rc("b", dense_score=0.8)]   # ranks 1, 2
    sparse = [("c", 5.0, 1), ("b", 4.0, 2)]                          # ranks 1, 2
    fused = hybrid.rrf_fuse(dense, sparse, {"c": _rc("c")}, settings)

    scores = {c.chunk_id: c.fused_score for c in fused}
    assert scores["a"] == pytest.approx(1 / 61)
    assert scores["c"] == pytest.approx(1 / 61)
    assert scores["b"] == pytest.approx(1 / 62 + 1 / 62)  # in BOTH lists → highest
    assert fused[0].chunk_id == "b"  # dual-list chunk outranks single-list chunks


def test_rrf_fuse_dedupes_and_populates_per_stage_scores():
    settings = _settings()
    dense = [_rc("shared", dense_score=0.7)]
    sparse = [("shared", 3.3, 1), ("sparse_only", 2.1, 2)]
    fused = hybrid.rrf_fuse(dense, sparse, {"sparse_only": _rc("sparse_only")}, settings)

    assert [c.chunk_id for c in fused].count("shared") == 1  # dedupe (AC-6)
    shared = next(c for c in fused if c.chunk_id == "shared")
    assert shared.dense_score == 0.7 and shared.sparse_score == 3.3  # both stages (AC-8)
    only = next(c for c in fused if c.chunk_id == "sparse_only")
    assert only.dense_score is None and only.sparse_score == 2.1
    assert all(c.fused_score is not None and c.rerank_score is None for c in fused)


def test_rrf_fuse_caps_at_fused_top_k():
    settings = _settings(HYBRID_FUSED_TOP_K=3)
    dense = [_rc(f"d{i}", dense_score=1.0 - i / 100) for i in range(10)]
    fused = hybrid.rrf_fuse(dense, [], {}, settings)
    assert len(fused) == 3


def test_rrf_fuse_drops_unhydratable_sparse_only():
    settings = _settings()
    fused = hybrid.rrf_fuse([], [("missing", 5.0, 1)], {}, settings)  # no hydration available
    assert fused == []


# -------------------------------------------------------------- T9: degraded mode (BM25-only)

async def test_hybrid_retrieve_degrades_to_bm25_on_dense_failure(monkeypatch):
    settings = _settings()

    async def _failing_dense(query, k, namespace, s):
        raise RuntimeError("pinecone down")

    async def _fake_load(s):
        return _bm25_cache(["fee refund policy", "probation rules"], ["a:0", "b:0"])

    async def _fake_hydrate(ids, namespace, s):
        return {cid: _rc(cid) for cid in ids}

    monkeypatch.setattr(retriever, "dense_retrieve", _failing_dense)
    monkeypatch.setattr(hybrid, "load_bm25", _fake_load)
    monkeypatch.setattr(hybrid, "hydrate_sparse_only", _fake_hydrate)

    chunks = await hybrid.hybrid_retrieve("fee refund", k=5, namespace=None, settings=settings)

    assert chunks  # BM25-only results, not a raise (AC-14)
    assert all(c.dense_score is None for c in chunks)
    assert hybrid.was_degraded() is True


async def test_hybrid_retrieve_healthy_is_not_degraded(monkeypatch):
    settings = _settings()

    async def _ok_dense(query, k, namespace, s):
        return [_rc("a:0", dense_score=0.9)]

    async def _fake_load(s):
        return _bm25_cache(["fee refund"], ["a:0"])

    async def _fake_hydrate(ids, namespace, s):
        return {}

    monkeypatch.setattr(retriever, "dense_retrieve", _ok_dense)
    monkeypatch.setattr(hybrid, "load_bm25", _fake_load)
    monkeypatch.setattr(hybrid, "hydrate_sparse_only", _fake_hydrate)

    await hybrid.hybrid_retrieve("fee refund", k=5, namespace=None, settings=settings)
    assert hybrid.was_degraded() is False


def test_was_degraded_reads_and_resets():
    hybrid._DEGRADED.set(True)
    assert hybrid.was_degraded() is True
    assert hybrid.was_degraded() is False  # reset, no leak across calls


# ----------------------------------------------------------- T10: fusion-safe refusal interaction

def test_refusal_gate_not_fired_when_sparse_only_tops_but_dense_supports():
    settings = _settings(REFUSAL_DENSE_THRESHOLD=0.25)
    # Fused order: a strong sparse-only hit at position 0 (dense_score None), a supporting dense
    # chunk above threshold deeper in the pool.
    chunks = [_rc("sparse_top"), _rc("dense_support", dense_score=0.6)]
    assert refusal.pre_llm_gate(chunks, settings) is False  # AC-15: no spurious refusal


def test_refusal_gate_fires_when_all_dense_below_threshold():
    settings = _settings(REFUSAL_DENSE_THRESHOLD=0.25)
    chunks = [_rc("s0"), _rc("d0", dense_score=0.1)]  # sparse-only + weak dense
    assert refusal.pre_llm_gate(chunks, settings) is True  # out-of-corpus protection intact


# ---------------------------------------------------------------- T7: dispatcher / baseline parity

async def test_dense_only_mode_is_identical_to_dense_retrieve(monkeypatch):
    settings = _settings()  # ENABLE_HYBRID False, RETRIEVAL_MODE None → dense_only
    sentinel = [_rc("d:0", dense_score=0.9)]

    async def _dense(query, k, namespace, s):
        return sentinel

    monkeypatch.setattr(retriever, "dense_retrieve", _dense)
    # hybrid must NOT be consulted in dense_only mode.
    monkeypatch.setattr(hybrid, "hybrid_retrieve",
                        lambda *a, **k: pytest.fail("hybrid used in dense_only mode"))

    out = await retriever.retrieve("q", k=5, namespace=None, settings=settings)
    # F6 unified the dispatcher to `pool[:k]`; dense_retrieve already returns ≤k, so the slice is a
    # content-preserving no-op — the baseline path is byte-for-byte identical in content/order (the
    # object-identity that held pre-F6 was incidental, not a product requirement (AC-11/F6 AC-17).
    assert [(c.chunk_id, c.dense_score) for c in out] == \
        [(c.chunk_id, c.dense_score) for c in sentinel]
    assert all(c.rerank_score is None for c in out)  # rerank off → no rerank applied


async def test_hybrid_mode_dispatches_and_truncates_to_k(monkeypatch):
    settings = _settings(ENABLE_HYBRID=True)
    pool = [_rc(f"c{i}", dense_score=1.0 - i / 100) for i in range(12)]

    async def _hybrid(query, k, namespace, s):
        return pool

    monkeypatch.setattr(hybrid, "hybrid_retrieve", _hybrid)
    out = await retriever.retrieve("q", k=5, namespace=None, settings=settings)
    assert [c.chunk_id for c in out] == [f"c{i}" for i in range(5)]  # top-k of the ≤12 pool (AC-9)


# ------------------------------------------------------------------------ T8: flag overlay

def test_apply_flags_maps_hybrid_and_does_not_mutate():
    settings = _settings()
    overlaid = flags_mod.apply_flags(settings, PipelineFlags(hybrid=True))
    assert overlaid.ENABLE_HYBRID is True
    assert settings.ENABLE_HYBRID is False  # original untouched (copy, not in-place)


def test_apply_flags_leaves_retrieval_mode_override_to_win():
    settings = _settings(RETRIEVAL_MODE="bm25_only")
    overlaid = flags_mod.apply_flags(settings, PipelineFlags(hybrid=True))
    assert retriever.resolve_mode(overlaid) == "bm25_only"  # eval override wins over the flag


async def test_eval_retrieval_suite_overlays_hybrid_flag_onto_seam():
    # The F4 retrieval suite calls the seam directly; F5 overlays the flag so the seam sees the
    # toggle without any change to how the suite scores (AC-12).
    from app.evals import retrieval as retrieval_suite
    from app.evals.schemas import EvalRecord

    seen = {}

    async def spy_retrieve(question, k, namespace, settings):
        seen["enable_hybrid"] = settings.ENABLE_HYBRID
        return []

    recs = [EvalRecord(qid="q1", question="fee refund", ground_truth_answer="a", tags=["en"])]
    await retrieval_suite.run_retrieval(
        recs, PipelineFlags(hybrid=True), _settings(), retrieve=spy_retrieve
    )
    assert seen["enable_hybrid"] is True


# ------------------------------------------------------------------------ T2: load fail-fast

async def test_load_bm25_missing_file_raises_hybrid_index_error(tmp_path):
    settings = _settings(BM25_PATH=tmp_path / "does_not_exist.pkl")
    with pytest.raises(hybrid.HybridIndexError, match="does_not_exist.pkl"):
        await hybrid.load_bm25(settings)


async def test_load_bm25_offloads_and_caches(monkeypatch, tmp_path):
    import pickle

    path = tmp_path / "bm25.pkl"
    payload = {"bm25": None, "chunk_ids": ["a:0"]}
    path.write_bytes(pickle.dumps(payload))
    settings = _settings(BM25_PATH=path)

    calls = {"n": 0}
    real_run_sync = hybrid.anyio.to_thread.run_sync

    async def _spy(fn, *args):
        calls["n"] += 1
        return await real_run_sync(fn, *args)

    monkeypatch.setattr(hybrid.anyio.to_thread, "run_sync", _spy)

    first = await hybrid.load_bm25(settings)
    second = await hybrid.load_bm25(settings)
    assert first == payload and second is first  # cached (AC-1)
    assert calls["n"] == 1  # loaded once, through the thread offload (AC-1/AC-19)
