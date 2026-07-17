"""F9 (T9): `rewrite.retrieve(rr=..., query_vec=...)` — the no-double-rewrite reuse, plus the
`flags.cache -> ENABLE_CACHE` overlay.

The cache key is F7's standalone normalized question, so the cache seam MUST rewrite before it can
look anything up. Without `rr` handoff, every cache miss would then pay for a SECOND gpt-4o-mini
rewrite inside `rewrite.retrieve` — doubling the rewrite cost and latency of the majority path.
`rewrite.py`'s module docstring declares it was decomposed for exactly this; these tests hold it to
that.
"""

import pytest

from app.core.contracts import PipelineFlags, RewriteResult
from app.core.settings import Settings
from app.rag import flags as flags_mod
from app.rag import retriever as retriever_mod
from app.rag import rewrite


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


@pytest.fixture
def spy(monkeypatch):
    """Counts rewrite calls; records the (query, query_vec) each fan-out pool was gathered for."""
    state = {"rewrites": 0, "pools": []}

    async def _fake_rewrite_query(query, memory, settings):
        state["rewrites"] += 1
        return RewriteResult(normalized="normalized q", variants=["v1", "v2"], language="en")

    async def _fake_pool(query, k, namespace, settings, query_vec=None):
        state["pools"].append((query, query_vec))
        return []

    async def _fake_retrieve(query, k, namespace, settings, query_vec=None):
        state["pools"].append((query, query_vec))
        return []

    monkeypatch.setattr(rewrite, "rewrite_query", _fake_rewrite_query)
    monkeypatch.setattr(retriever_mod, "gather_candidate_pool", _fake_pool)
    monkeypatch.setattr(retriever_mod, "retrieve", _fake_retrieve)
    return state


# --------------------------------------------------------------- no double rewrite (AC-12)

async def test_supplying_rr_skips_the_rewrite_call(spy):
    rr = RewriteResult(normalized="already rewritten", variants=[], language="en")

    await rewrite.retrieve("raw q", 5, "pu", _settings(ENABLE_QUERY_REWRITE=True), None, rr=rr)

    assert spy["rewrites"] == 0, "the cache seam already paid for the rewrite; this would double it"
    assert [q for q, _ in spy["pools"]] == ["already rewritten"]


async def test_omitting_rr_preserves_todays_behaviour(spy):
    await rewrite.retrieve("raw q", 5, "pu", _settings(ENABLE_QUERY_REWRITE=True), None)

    assert spy["rewrites"] == 1
    assert [q for q, _ in spy["pools"]] == ["normalized q", "v1", "v2"]


async def test_supplied_rr_is_still_published_to_last_rewrite(spy):
    """`_pipeline_events` reads `last_rewrite()` out-of-band for the answer-language directive —
    handing in `rr` must not break that."""
    rr = RewriteResult(normalized="already rewritten", variants=[], language="ur-mix")

    await rewrite.retrieve("raw q", 5, "pu", _settings(ENABLE_QUERY_REWRITE=True), None, rr=rr)

    published = rewrite.last_rewrite()
    assert published is not None
    assert published.normalized == "already rewritten"
    assert published.language == "ur-mix"


# --------------------------------------------------------------- vector routing (AC-11)

async def test_query_vec_goes_only_to_the_normalized_fanout_query(spy):
    """The variants are DIFFERENT strings — handing them the normalized query's vector would
    retrieve the wrong neighbourhood while looking perfectly healthy."""
    vec = [0.1, 0.2, 0.3]

    await rewrite.retrieve("raw q", 5, "pu", _settings(ENABLE_QUERY_REWRITE=True), None,
                           query_vec=vec)

    pools = dict(spy["pools"])
    assert pools["normalized q"] == vec
    assert pools["v1"] is None
    assert pools["v2"] is None


async def test_query_vec_threads_through_the_rewrite_off_path(spy):
    await rewrite.retrieve("raw q", 5, "pu", _settings(ENABLE_QUERY_REWRITE=False), None,
                           query_vec=[0.9])

    assert spy["rewrites"] == 0
    assert spy["pools"] == [("raw q", [0.9])]


async def test_rewrite_off_without_vector_is_unchanged(spy):
    await rewrite.retrieve("raw q", 5, "pu", _settings(ENABLE_QUERY_REWRITE=False), None)

    assert spy["pools"] == [("raw q", None)]
    assert rewrite.last_rewrite() is None


# --------------------------------------------------------------- flag overlay (AC-31)

def test_flags_cache_maps_onto_enable_cache():
    s = _settings(ENABLE_CACHE=False)
    assert flags_mod.apply_flags(s, PipelineFlags(cache=True)).ENABLE_CACHE is True
    assert flags_mod.apply_flags(s, PipelineFlags(cache=False)).ENABLE_CACHE is False


def test_apply_flags_still_maps_every_earlier_toggle():
    s = _settings()
    out = flags_mod.apply_flags(
        s, PipelineFlags(hybrid=True, rerank=True, query_rewrite=True, compression=True, cache=True)
    )
    assert (out.ENABLE_HYBRID, out.ENABLE_RERANK, out.ENABLE_QUERY_REWRITE,
            out.ENABLE_COMPRESSION, out.ENABLE_CACHE) == (True, True, True, True, True)


def test_apply_flags_does_not_mutate_the_input():
    s = _settings(ENABLE_CACHE=False)
    flags_mod.apply_flags(s, PipelineFlags(cache=True))
    assert s.ENABLE_CACHE is False
