"""F9 (T10/T11): the cache seam inside `_pipeline_events` — toggle parity, hit shape, write-behind.

The most important test here is `test_cache_off_is_byte_for_byte_the_f8_path`: F9 must be invisible
when `ENABLE_CACHE` is false, or `f8-compression-after`'s numbers stop being a valid baseline and
the prod rollback story is a lie.
"""

import asyncio

import pytest
from sqlalchemy import func, select

from app.caching import store
from app.core.contracts import PipelineFlags
from app.db.models.ops import CacheEntry
from app.rag import baseline
from app.rag import retriever as retriever_mod
from app.rag import rewrite as rewrite_mod
from tests.cache.conftest import MANIFEST, unit

ANSWER = "Probation is cleared at CGPA 2.0 [1]."


class FakeChunk:
    pass


def _chunk(score=0.9):
    from app.core.contracts import RetrievedChunk

    return RetrievedChunk(chunk_id="d:0", doc_id="d", title="PU Calendar", text="CGPA 2.0 clears.",
                          dense_score=score, rerank_score=score)


def _fake_llm(text):
    class FakeStream:
        async def astream_events(self, chain_input, version, config):
            for tok in text.split(" "):
                yield {"event": "on_chat_model_stream",
                       "data": {"chunk": type("C", (), {"content": tok + " "})()}}

    def _build(settings):
        return FakeStream()

    return _build


@pytest.fixture
def pipeline(monkeypatch, session):
    """A fully faked pipeline: no Pinecone, no OpenAI, no cross-encoder. Records embed calls so the
    single-embed-per-request property is observable."""
    state = {"embeds": 0, "retrievals": 0, "llm_calls": 0}

    async def _fake_retrieve(query, k, namespace, settings, memory=None, rr=None, query_vec=None):
        state["retrievals"] += 1
        return [_chunk()]

    async def _fake_embed(query, settings):
        state["embeds"] += 1
        return unit(1.0)

    class FakeChain:
        async def astream_events(self, chain_input, version, config):
            state["llm_calls"] += 1
            for tok in ANSWER.split(" "):
                yield {"event": "on_chat_model_stream",
                       "data": {"chunk": type("C", (), {"content": tok + " "})()}}

    async def _fake_parse(answer_text, chunks, sess):
        from app.core.contracts import Citation

        return [Citation(chunk_id="d:0", doc_id="d", title="PU Calendar", url="http://x",
                         quote="CGPA 2.0")]

    async def _fake_manifest(settings):
        return MANIFEST

    monkeypatch.setattr(rewrite_mod, "retrieve", _fake_retrieve)
    monkeypatch.setattr(retriever_mod, "compute_query_vector", _fake_embed)
    monkeypatch.setattr(baseline, "build_generate_chain", lambda llm: FakeChain())
    monkeypatch.setattr(baseline, "build_llm", lambda settings: object())
    monkeypatch.setattr(baseline.citations_mod, "parse_citations", _fake_parse)
    monkeypatch.setattr(store, "manifest_id", _fake_manifest)
    return state


async def _collect(agen):
    return [ev async for ev in agen]


async def _ask(question, settings, session, sessionmaker_, flags=None):
    return await _collect(baseline.astream(
        question, 5, None, flags or PipelineFlags(cache=settings.ENABLE_CACHE),
        session=session, settings=settings, sessionmaker=sessionmaker_,
    ))


# --------------------------------------------------------------- toggle parity (AC-30)

async def test_cache_off_is_byte_for_byte_the_f8_path(pipeline, session, sessionmaker_,
                                                      cache_settings):
    """No cache_lookup stage, no embed, no cache row — F9 is invisible when off."""
    settings = cache_settings.model_copy(update={"ENABLE_CACHE": False})

    events = await _ask("how do i get off probation", settings, session, sessionmaker_)
    await store.drain_writes()

    stages = [e.data["stage"] for e in events if e.event == "stage"]
    assert "cache_lookup" not in stages
    # rewriting/reranking/compressing each emit a single `skipped` frame when their flag is off,
    # so a disabled layer is visible in the UI as deliberately-not-run rather than absent.
    assert stages == ["rewriting", "searching", "searching", "reranking", "compressing",
                      "generating", "generating", "citing", "citing"]
    assert pipeline["embeds"] == 0, "cache off must not add an embed to the request path"
    async with sessionmaker_() as s:
        assert await s.scalar(select(func.count()).select_from(CacheEntry)) == 0


# --------------------------------------------------------------- miss then hit

async def test_miss_answers_normally_and_writes_behind(pipeline, session, sessionmaker_,
                                                       cache_settings):
    events = await _ask("how do i get off probation", cache_settings, session, sessionmaker_)
    await store.drain_writes()

    kinds = [e.event for e in events]
    assert kinds[0] == "stage" and events[0].data["stage"] == "cache_lookup"
    assert pipeline["llm_calls"] == 1  # a miss still generates
    assert pipeline["embeds"] == 1, "exactly ONE embed per request (AC-5)"

    meta = next(e for e in events if e.event == "meta")
    assert meta.data["cache_hit"] is False
    assert meta.data["tokens_in"] > 0 and meta.data["tokens_out"] > 0  # AC-27b

    async with sessionmaker_() as s:
        assert await s.scalar(select(func.count()).select_from(CacheEntry)) == 1


async def test_second_identical_ask_hits_the_cache(pipeline, session, sessionmaker_,
                                                   cache_settings):
    await _ask("how do i get off probation", cache_settings, session, sessionmaker_)
    await store.drain_writes()
    before = dict(pipeline)

    events = await _ask("how do i get off probation", cache_settings, session, sessionmaker_)

    meta = next(e for e in events if e.event == "meta")
    assert meta.data["cache_hit"] is True
    assert pipeline["llm_calls"] == before["llm_calls"], "a hit must not call the LLM"
    assert pipeline["retrievals"] == before["retrievals"], "a hit must not retrieve"


async def test_hit_event_shape(pipeline, session, sessionmaker_, cache_settings):
    """AC-24: the ordered SSE contract holds, with no new event type — the hit reuses the refusal
    path's skipped-stage shape so F14 needs no change."""
    await _ask("how do i get off probation", cache_settings, session, sessionmaker_)
    await store.drain_writes()

    events = await _ask("how do i get off probation", cache_settings, session, sessionmaker_)

    kinds = [e.event for e in events]
    assert kinds == ["stage"] * 7 + ["token", "citations", "meta", "done"]
    stages = [(e.data["stage"], e.data["status"]) for e in events if e.event == "stage"]
    # The replay marks every pipeline layer skipped, including the two F6/F8 stages added later —
    # a hit still needs no new event TYPE, which is what AC-24 actually pins.
    assert stages == [("cache_lookup", "started"), ("cache_lookup", "done"),
                      ("searching", "skipped"), ("reranking", "skipped"),
                      ("compressing", "skipped"), ("generating", "skipped"),
                      ("citing", "skipped")]
    assert next(e for e in events if e.event == "citations").data["citations"]


async def test_hit_replays_the_answer_as_one_token_event(pipeline, session, sessionmaker_,
                                                         cache_settings):
    miss = await _ask("how do i get off probation", cache_settings, session, sessionmaker_)
    await store.drain_writes()
    miss_text = "".join(e.data["token"] for e in miss if e.event == "token")

    hit = await _ask("how do i get off probation", cache_settings, session, sessionmaker_)
    hit_tokens = [e.data["token"] for e in hit if e.event == "token"]

    assert len(hit_tokens) == 1, "a hit replays the whole answer in one token event"
    assert hit_tokens[0] == miss_text, "byte-for-byte, disclaimer included (AC-25)"


async def test_astream_and_answer_agree_on_a_hit(pipeline, session, sessionmaker_,
                                                 cache_settings):
    """AC-25: `answer()` reassembles from token events, so a hit must reassemble identically."""
    await _ask("how do i get off probation", cache_settings, session, sessionmaker_)
    await store.drain_writes()

    streamed = await _ask("how do i get off probation", cache_settings, session, sessionmaker_)
    streamed_text = "".join(e.data["token"] for e in streamed if e.event == "token")

    collected = await baseline.answer(
        "how do i get off probation", 5, None, PipelineFlags(cache=True),
        session=session, settings=cache_settings, sessionmaker=sessionmaker_,
    )

    assert collected.answer == streamed_text
    assert collected.cache_hit is True


# --------------------------------------------------------------- write gating (AC-16)

async def test_refused_answer_is_not_cached(pipeline, session, sessionmaker_, cache_settings,
                                            monkeypatch):
    """A refusal is not an answer. Caching one would serve 'I don't know' for 24h."""
    async def _low_score_retrieve(query, k, namespace, settings, memory=None, rr=None,
                                  query_vec=None):
        return [_chunk(score=0.001)]

    monkeypatch.setattr(rewrite_mod, "retrieve", _low_score_retrieve)

    events = await _ask("something out of corpus", cache_settings, session, sessionmaker_)
    await store.drain_writes()

    meta = next(e for e in events if e.event == "meta")
    assert meta.data["refused"] is True
    async with sessionmaker_() as s:
        assert await s.scalar(select(func.count()).select_from(CacheEntry)) == 0


async def test_degraded_answer_is_not_cached(pipeline, session, sessionmaker_, cache_settings,
                                             monkeypatch):
    """A degraded answer came from BM25-only; caching it would freeze a known-weaker answer in at
    full confidence for 24h."""
    monkeypatch.setattr(baseline.hybrid_mod, "was_degraded", lambda: True)

    await _ask("how do i get off probation", cache_settings, session, sessionmaker_)
    await store.drain_writes()

    async with sessionmaker_() as s:
        assert await s.scalar(select(func.count()).select_from(CacheEntry)) == 0


async def test_zero_citation_answer_is_not_cached(pipeline, session, sessionmaker_,
                                                  cache_settings, monkeypatch):
    async def _no_citations(answer_text, chunks, sess):
        return []

    monkeypatch.setattr(baseline.citations_mod, "parse_citations", _no_citations)

    await _ask("how do i get off probation", cache_settings, session, sessionmaker_)
    await store.drain_writes()

    async with sessionmaker_() as s:
        assert await s.scalar(select(func.count()).select_from(CacheEntry)) == 0


async def test_a_hit_does_not_rewrite_itself_back_into_the_cache(pipeline, session, sessionmaker_,
                                                                 cache_settings):
    await _ask("how do i get off probation", cache_settings, session, sessionmaker_)
    await store.drain_writes()
    await _ask("how do i get off probation", cache_settings, session, sessionmaker_)
    await store.drain_writes()

    async with sessionmaker_() as s:
        assert await s.scalar(select(func.count()).select_from(CacheEntry)) == 1


# --------------------------------------------------------------- write-behind timing (AC-15)

async def test_write_does_not_delay_the_done_event(pipeline, session, sessionmaker_,
                                                   cache_settings, monkeypatch):
    """The whole reason the write is a task and not an await."""
    started = asyncio.Event()
    release = asyncio.Event()

    original = store._CACHE.write

    async def _slow_write(*a, **kw):
        started.set()
        await release.wait()
        return await original(*a, **kw)

    monkeypatch.setattr(store._CACHE, "write", _slow_write)

    events = await _ask("how do i get off probation", cache_settings, session, sessionmaker_)

    # The stream is fully delivered — `done` included — while the write is still blocked. In fact
    # the write has not even been given the loop yet: `create_task` only schedules it.
    assert events[-1].event == "done"
    assert not started.is_set(), "the write ran inline — it must be scheduled, not awaited"

    await asyncio.sleep(0)  # hand the loop to the task
    assert started.is_set(), "the scheduled write never started"
    assert store._WRITE_TASKS, "the task must be strongly referenced while in flight"

    release.set()
    await store.drain_writes()

    async with sessionmaker_() as s:
        assert await s.scalar(select(func.count()).select_from(CacheEntry)) == 1


# --------------------------------------------------------------- fail-open (AC-10)

async def test_cache_backend_failure_still_answers(pipeline, session, cache_settings):
    """A cache outage costs a cache hit, never the request."""
    def _broken_sessionmaker():
        raise RuntimeError("postgres is on fire")

    events = await _collect(baseline.astream(
        "how do i get off probation", 5, None, PipelineFlags(cache=True),
        session=session, settings=cache_settings, sessionmaker=_broken_sessionmaker,
    ))

    assert events[-1].event == "done"
    meta = next(e for e in events if e.event == "meta")
    assert meta.data["cache_hit"] is False
    assert pipeline["llm_calls"] == 1  # answered normally
