"""Semantic tier: write, upsert, capacity eviction, flush, poison control, and the write-behind
seam. Split from `test_store.py` (lookup) to keep each file readable.
"""

import asyncio

from sqlalchemy import func, select

from app.caching import store
from app.caching.keys import exact_key, normalize
from app.db.models.ops import CacheEntry
from tests.cache.conftest import MANIFEST, make_answer, unit


async def _count(sessionmaker_) -> int:
    async with sessionmaker_() as s:
        return int(await s.scalar(select(func.count()).select_from(CacheEntry)) or 0)


# --------------------------------------------------------------- write round-trip

async def test_write_then_lookup_round_trips_the_response(sessionmaker_, settings_with_manifest):
    vec = unit(1.0)
    original = make_answer("Probation clears at CGPA 2.0 [1].")

    await store.write("how do i get off probation", vec, original,
                      settings=settings_with_manifest, sessionmaker=sessionmaker_)
    result = await store.lookup("how do i get off probation", vec,
                                settings=settings_with_manifest, sessionmaker=sessionmaker_)

    assert result is not None
    cached, _ = result
    assert cached.answer == original.answer
    assert cached.citations == original.citations  # citations survive the JSONB round-trip
    assert cached.tokens_in == original.tokens_in
    assert cached.tokens_out == original.tokens_out


async def test_write_stores_the_normalized_query_and_its_hash(sessionmaker_,
                                                              settings_with_manifest):
    await store.write("how do i get off probation", unit(1.0), make_answer(),
                      settings=settings_with_manifest, sessionmaker=sessionmaker_)

    async with sessionmaker_() as s:
        row = await s.scalar(select(CacheEntry))
    assert row.query_text == "how do i get off probation"
    assert row.query_hash == exact_key(normalize("how do i get off probation"))
    assert row.index_manifest_id == MANIFEST
    assert row.hits == 0


# --------------------------------------------------------------- upsert (AC-17)

async def test_writing_the_same_query_twice_upserts_rather_than_duplicating(
    sessionmaker_, settings_with_manifest
):
    """Without this, every repeat ask inserts a row and the brute-force matrix grows unbounded —
    which is what the unique constraint in migration 0003 is defending."""
    vec = unit(1.0)
    await store.write("same question", vec, make_answer("first answer [1]."),
                      settings=settings_with_manifest, sessionmaker=sessionmaker_)
    await store.write("same question", vec, make_answer("second answer [1]."),
                      settings=settings_with_manifest, sessionmaker=sessionmaker_)

    assert await _count(sessionmaker_) == 1
    assert len(store._CACHE._ids) == 1, "the matrix must not grow a duplicate row either"

    cached, _ = await store.lookup("same question", vec, settings=settings_with_manifest,
                                   sessionmaker=sessionmaker_)
    assert cached.answer == "second answer [1]."


# --------------------------------------------------------------- capacity (AC-18)

async def test_at_capacity_write_evicts_least_recently_hit(sessionmaker_, cache_settings,
                                                           monkeypatch):
    async def _fake_manifest_id(settings):
        return MANIFEST

    monkeypatch.setattr(store, "manifest_id", _fake_manifest_id)
    settings = cache_settings.model_copy(update={"CACHE_MAX_ENTRIES": 2})

    await store.write("query one", unit(1.0), make_answer("one"),
                      settings=settings, sessionmaker=sessionmaker_)
    await store.write("query two", unit(0.0, 1.0), make_answer("two"),
                      settings=settings, sessionmaker=sessionmaker_)

    # Hit "query one" so "query two" becomes the coldest entry.
    await store.lookup("query one", unit(1.0), settings=settings, sessionmaker=sessionmaker_)

    await store.write("query three", unit(0.0, 0.0, 1.0), make_answer("three"),
                      settings=settings, sessionmaker=sessionmaker_)

    assert await _count(sessionmaker_) == 2, "capacity must hold at CACHE_MAX_ENTRIES"
    assert len(store._CACHE._ids) == 2
    async with sessionmaker_() as s:
        remaining = {r.query_text for r in (await s.scalars(select(CacheEntry))).all()}
    assert remaining == {"query one", "query three"}, "the never-hit entry should be evicted"


async def test_upsert_at_capacity_does_not_evict(sessionmaker_, cache_settings, monkeypatch):
    """An update replaces a row in place, so it cannot grow the matrix and must not cost an
    unrelated entry its slot."""
    async def _fake_manifest_id(settings):
        return MANIFEST

    monkeypatch.setattr(store, "manifest_id", _fake_manifest_id)
    settings = cache_settings.model_copy(update={"CACHE_MAX_ENTRIES": 2})

    await store.write("query one", unit(1.0), make_answer("one"),
                      settings=settings, sessionmaker=sessionmaker_)
    await store.write("query two", unit(0.0, 1.0), make_answer("two"),
                      settings=settings, sessionmaker=sessionmaker_)
    await store.write("query one", unit(1.0), make_answer("one updated"),
                      settings=settings, sessionmaker=sessionmaker_)

    assert await _count(sessionmaker_) == 2
    async with sessionmaker_() as s:
        remaining = {r.query_text for r in (await s.scalars(select(CacheEntry))).all()}
    assert remaining == {"query one", "query two"}


# --------------------------------------------------------------- flush (AC-20)

async def test_flush_empties_both_tiers_and_returns_the_count(sessionmaker_,
                                                              settings_with_manifest):
    await store.write("query one", unit(1.0), make_answer(),
                      settings=settings_with_manifest, sessionmaker=sessionmaker_)
    await store.write("query two", unit(0.0, 1.0), make_answer(),
                      settings=settings_with_manifest, sessionmaker=sessionmaker_)

    deleted = await store.flush(settings=settings_with_manifest, sessionmaker=sessionmaker_)

    assert deleted == 2
    assert await _count(sessionmaker_) == 0
    assert store._CACHE._matrix is None  # reset, so the next lookup rebuilds from an empty table
    assert await store.lookup("query one", unit(1.0), settings=settings_with_manifest,
                              sessionmaker=sessionmaker_) is None


# --------------------------------------------------------------- poison control (AC-21)

async def test_delete_by_query_removes_one_entry_then_reports_zero(sessionmaker_,
                                                                   settings_with_manifest):
    await store.write("bad answer question", unit(1.0), make_answer(),
                      settings=settings_with_manifest, sessionmaker=sessionmaker_)
    await store.write("unrelated question", unit(0.0, 1.0), make_answer(),
                      settings=settings_with_manifest, sessionmaker=sessionmaker_)

    assert await store.delete_by_query("bad answer question", settings=settings_with_manifest,
                                       sessionmaker=sessionmaker_) == 1
    assert await store.delete_by_query("bad answer question", settings=settings_with_manifest,
                                       sessionmaker=sessionmaker_) == 0

    assert await _count(sessionmaker_) == 1
    assert await store.lookup("bad answer question", unit(1.0),
                              settings=settings_with_manifest, sessionmaker=sessionmaker_) is None


async def test_delete_by_query_normalizes_so_the_operator_can_paste_the_raw_question(
    sessionmaker_, settings_with_manifest
):
    await store.write("bad answer question", unit(1.0), make_answer(),
                      settings=settings_with_manifest, sessionmaker=sessionmaker_)

    # Operator pastes the question as the student typed it, not pre-normalized.
    assert await store.delete_by_query("  Bad Answer Question?  ",
                                       settings=settings_with_manifest,
                                       sessionmaker=sessionmaker_) == 1


# --------------------------------------------------------------- write-behind seam (AC-14/19)

async def test_schedule_write_lands_the_entry(sessionmaker_, settings_with_manifest):
    task = store.schedule_write("deferred question", unit(1.0), make_answer(),
                                settings=settings_with_manifest, sessionmaker=sessionmaker_)
    await store.drain_writes()

    assert task.done()
    assert await _count(sessionmaker_) == 1


async def test_schedule_write_holds_a_strong_reference(sessionmaker_, settings_with_manifest):
    """asyncio holds only a weak ref to a running task; without the module-level set the write can
    be GC'd mid-await and the entry silently never lands."""
    store.schedule_write("q", unit(1.0), make_answer(),
                         settings=settings_with_manifest, sessionmaker=sessionmaker_)
    assert len(store._WRITE_TASKS) == 1

    await store.drain_writes()
    await asyncio.sleep(0)  # let the done-callback run
    assert len(store._WRITE_TASKS) == 0, "completed tasks must be discarded, not leaked"


async def test_failing_write_is_logged_and_swallowed(settings_with_manifest):
    """A cache write failure must never surface to the client or crash the loop (AC-19)."""
    def _broken_sessionmaker():
        raise RuntimeError("postgres is on fire")

    task = store.schedule_write("q", unit(1.0), make_answer(),
                                settings=settings_with_manifest,
                                sessionmaker=_broken_sessionmaker)
    await store.drain_writes()

    assert task.done()
    assert task.exception() is None, "the task must swallow the error, not park an exception"
