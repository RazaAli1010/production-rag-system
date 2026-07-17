"""Semantic tier: matrix load + the two-signal accept rule + lazy manifest expiry.

Vector helpers, the pinned manifest and the sample answer live in `conftest.py` — see the note
there on why these vectors are synthetic rather than real embeddings.
"""

import asyncio

import pytest

from app.caching import store
from app.caching.keys import exact_key, normalize
from app.core.contracts import AnswerResponse
from app.db.models.ops import CacheEntry
from tests.cache.conftest import MANIFEST, make_answer, rotated, unit


async def _seed(session, query: str, vec: list[float], *, manifest: str = MANIFEST,
                answer: AnswerResponse | None = None) -> CacheEntry:
    n = normalize(query)
    row = CacheEntry(
        query_hash=exact_key(n),
        query_text=n,
        embedding=store.pack_vector(vec),
        answer=(answer or make_answer()).model_dump(),
        index_manifest_id=manifest,
        hits=0,
    )
    session.add(row)
    await session.commit()
    return row


# --------------------------------------------------------------- hit (AC-6/AC-7)

async def test_identical_vector_hits(session, sessionmaker_, settings_with_manifest):
    vec = unit(1.0)
    await _seed(session, "how do i get off probation", vec)

    result = await store.lookup("how do i get off probation", vec,
                                settings=settings_with_manifest, sessionmaker=sessionmaker_)

    assert result is not None
    answer, cosine = result
    assert cosine == pytest.approx(1.0, abs=1e-5)
    assert answer.answer == "Probation is cleared at CGPA 2.0 [1]."
    assert answer.citations[0].chunk_id == "d:0"
    assert answer.tokens_in == 1200  # what the hit avoided spending (AC-27b)


async def test_hit_increments_hits_and_sets_last_hit_at(session, sessionmaker_,
                                                        settings_with_manifest):
    vec = unit(1.0)
    row = await _seed(session, "how do i get off probation", vec)
    assert row.hits == 0 and row.last_hit_at is None

    await store.lookup("how do i get off probation", vec,
                       settings=settings_with_manifest, sessionmaker=sessionmaker_)

    await session.refresh(row)
    assert row.hits == 1
    assert row.last_hit_at is not None


# --------------------------------------------------------------- cosine floor (AC-6)

async def test_below_threshold_misses(session, sessionmaker_, settings_with_manifest):
    base = unit(1.0)
    await _seed(session, "how do i get off probation", base)

    result = await store.lookup("how do i get off probation", rotated(base, 0.80),
                                settings=settings_with_manifest, sessionmaker=sessionmaker_)

    assert result is None  # 0.80 < CACHE_SIMILARITY_THRESHOLD (0.86)


# --------------------------------------------------------------- discriminative veto (AC-8)

async def test_degree_level_disagreement_vetoes_even_at_high_cosine(session, sessionmaker_,
                                                                    settings_with_manifest):
    """The veto's reason for existing: embedding similarity says 'same topic', which is not the
    same claim as 'same answer'. Even at 0.99 cosine, BS and MPhil are different questions."""
    vec = unit(1.0)
    await _seed(session, "bs admission deadline", vec)

    result = await store.lookup("mphil admission deadline", rotated(vec, 0.99),
                                settings=settings_with_manifest, sessionmaker=sessionmaker_)

    assert result is None


async def test_section_id_disagreement_vetoes(session, sessionmaker_, settings_with_manifest):
    """15(3) vs 15(4) is the worst real pair in the calibration set (0.930 cosine) — higher than
    any true paraphrase. Only the veto catches it."""
    vec = unit(1.0)
    await _seed(session, "what does regulation 15(3) say", vec)

    assert await store.lookup("what does regulation 15(4) say", rotated(vec, 0.99),
                              settings=settings_with_manifest, sessionmaker=sessionmaker_) is None


async def test_year_disagreement_vetoes(session, sessionmaker_, settings_with_manifest):
    vec = unit(1.0)
    await _seed(session, "2023 fee schedule", vec)

    assert await store.lookup("2024 fee schedule", rotated(vec, 0.99),
                              settings=settings_with_manifest, sessionmaker=sessionmaker_) is None


async def test_shared_discriminators_do_not_veto(session, sessionmaker_,
                                                 settings_with_manifest):
    """The veto must only fire on DISAGREEMENT — two questions that both say 'bs' are still
    cacheable against each other, or the veto would reject every real paraphrase too."""
    vec = unit(1.0)
    await _seed(session, "what is the bs admission deadline", vec)

    result = await store.lookup("when is the bs admission deadline", rotated(vec, 0.99),
                                settings=settings_with_manifest, sessionmaker=sessionmaker_)

    assert result is not None


async def test_paraphrase_without_discriminators_hits(session, sessionmaker_,
                                                      settings_with_manifest):
    """The case the semantic tier exists for: no discriminating token on either side, cosine above
    the floor."""
    vec = unit(1.0)
    await _seed(session, "how do i get off academic probation", vec)

    result = await store.lookup("what is the process to clear academic probation",
                                rotated(vec, 0.88), settings=settings_with_manifest,
                                sessionmaker=sessionmaker_)

    assert result is not None


# --------------------------------------------------------------- manifest expiry (AC-9)

async def test_stale_manifest_misses_and_deletes_the_entry(session, sessionmaker_,
                                                           settings_with_manifest):
    vec = unit(1.0)
    row = await _seed(session, "how do i get off probation", vec, manifest="old-manifest")
    entry_id = row.id

    result = await store.lookup("how do i get off probation", vec,
                                settings=settings_with_manifest, sessionmaker=sessionmaker_)

    assert result is None
    async with sessionmaker_() as s:
        assert await s.get(CacheEntry, entry_id) is None, "stale entry must be deleted, not kept"
    assert entry_id not in store._CACHE._ids, "stale entry must leave the matrix too"


# --------------------------------------------------------------- cold / empty

async def test_empty_cache_returns_none(sessionmaker_, settings_with_manifest):
    assert await store.lookup("anything", unit(1.0),
                              settings=settings_with_manifest, sessionmaker=sessionmaker_) is None


async def test_zero_vector_returns_none(session, sessionmaker_, settings_with_manifest):
    await _seed(session, "how do i get off probation", unit(1.0))
    assert await store.lookup("how do i get off probation", [0.0] * 1536,
                              settings=settings_with_manifest, sessionmaker=sessionmaker_) is None


# --------------------------------------------------------------- fail-open (AC-10)

async def test_backend_error_degrades_to_miss_not_an_exception(settings_with_manifest):
    """A cache backend error must cost a cache hit, never the request."""
    def _broken_sessionmaker():
        raise RuntimeError("postgres is on fire")

    result = await store.lookup("q", unit(1.0), settings=settings_with_manifest,
                                sessionmaker=_broken_sessionmaker)
    assert result is None


# --------------------------------------------------------------- matrix load (AC-22)

async def test_concurrent_first_lookups_load_the_matrix_once(session, sessionmaker_,
                                                             settings_with_manifest, monkeypatch):
    await _seed(session, "how do i get off probation", unit(1.0))
    await store.reset()

    loads = []
    original = store.SemanticCache._ensure_loaded

    async def _counting(self, **kw):
        result = await original(self, **kw)
        loads.append(1)
        return result

    monkeypatch.setattr(store.SemanticCache, "_ensure_loaded", _counting)

    await asyncio.gather(*(
        store.lookup("how do i get off probation", unit(1.0),
                     settings=settings_with_manifest, sessionmaker=sessionmaker_)
        for _ in range(5)
    ))

    # _ensure_loaded is called 5x but must only READ Postgres once — the second caller through the
    # lock sees the matrix already built.
    assert store._CACHE._matrix is not None
    assert len(store._CACHE._ids) == 1


async def test_matrix_rebuilds_from_postgres_after_reset(session, sessionmaker_,
                                                         settings_with_manifest):
    """The matrix is a derived view of Postgres, never the source of truth — which is what makes
    drift across a restart impossible."""
    await _seed(session, "how do i get off probation", unit(1.0))

    await store.lookup("how do i get off probation", unit(1.0),
                       settings=settings_with_manifest, sessionmaker=sessionmaker_)
    assert len(store._CACHE._ids) == 1

    await store.reset()
    assert store._CACHE._matrix is None

    result = await store.lookup("how do i get off probation", unit(1.0),
                                settings=settings_with_manifest, sessionmaker=sessionmaker_)
    assert result is not None  # rebuilt from Postgres, not from memory


async def test_vector_round_trips_through_bytea(session, sessionmaker_, settings_with_manifest):
    vec = unit(0.6, 0.8)
    await _seed(session, "q", vec)
    await store.lookup("q", vec, settings=settings_with_manifest, sessionmaker=sessionmaker_)

    packed = store.pack_vector(vec)
    assert len(packed) == 1536 * 4
    assert store.unpack_vector(packed)[:2] == pytest.approx([0.6, 0.8], abs=1e-6)
