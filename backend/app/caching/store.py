"""Semantic tier — the in-memory cosine matrix over Postgres `cache_entries` (design.md §3/§5).

**Why brute force and no vector index.** `cache_entries` stays under `CACHE_MAX_ENTRIES` (10k) by
construction, and 10k × 1536 float32 is 61 MB resident with a sub-millisecond matmul. Pinecone is
the vector store for the corpus; adding a second vector index — or pgvector, which the fixed stack
bans — to search ten thousand rows would be strictly more machinery for strictly less speed.

**The accept rule is two-signal, and that is the whole feature.** Cosine ≥ threshold says *these
questions are about the same thing*; it does NOT say *these questions have the same answer*. The two
sets OVERLAP on cosine — measured at T7 against real `text-embedding-3-small` vectors, the worst
adversarial pair ("regulation 15(3)" vs "15(4)", 0.930) scores HIGHER than the best true paraphrase
(0.912) — so no cosine threshold separates them and cosine alone can never be the rule. The
discriminative-token veto (`keys.discriminators`) is what separates them; see `keys.py` for why a
Jaccard floor was specced, measured to contribute nothing, and dropped.

Both the threshold (0.86) and the veto are calibrated against
`tests/fixtures/cache/adversarial.jsonl`, not guessed. The surviving margin is 0.032, which is thin
— that is why `ENABLE_CACHE` ships default-off and the Redis exact-match tier carries the value.

**Everything here is fail-open.** A cache backend error degrades to a miss and the request answers
normally (AC-10/AC-19) — the cache is an optimization, never a failure source.

Async-mandate placement (CLAUDE.md "which side of the line"): the Postgres reads/writes are awaited
async SQLAlchemy. The cosine matmul is cheap pure-CPU and runs INLINE — CLAUDE.md names "cosine
matmul on the cache matrix" explicitly as the inline side of the line. At 10k × 1536 it is ~15M
FLOPs, sub-ms in BLAS; handing it to `anyio.to_thread` would cost more in context switching than it
saves.
"""

import asyncio
import struct
from datetime import UTC, datetime

import numpy as np
import structlog
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select

from app.caching import keys, redis_hot
from app.core.contracts import AnswerResponse
from app.db.models.ops import CacheEntry
from app.indexing.manifest import manifest_id

logger = structlog.get_logger(__name__)


def pack_vector(vec: list[float]) -> bytes:
    """float32[] -> BYTEA, the encoding F12's schema documents and `tests/db/test_models_ops.py`
    round-trips."""
    return struct.pack(f"<{len(vec)}f", *vec)


def unpack_vector(raw: bytes) -> np.ndarray:
    return np.frombuffer(raw, dtype="<f4")


def _l2_normalize(m: np.ndarray) -> np.ndarray:
    """Row-normalize so cosine similarity is a plain dot product. Zero-norm rows (a degenerate
    embedding) are left at zero rather than producing NaN — they then score 0.0 against everything,
    which is the correct "never matches" behaviour."""
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    return np.divide(m, norms, out=np.zeros_like(m), where=norms > 0)


class SemanticCache:
    """One instance per process (`_CACHE` below).

    `_ids` / `_query_texts` / `_discriminators` / `_manifests` are ROW-PARALLEL with `_matrix`'s
    rows: index
    `i` in each describes matrix row `i`. They are only ever mutated together, under `_lock` — the
    one invariant that makes the matmul's `argmax` meaningful. Never re-zip them from separate
    queries.
    """

    def __init__(self) -> None:
        self._matrix: np.ndarray | None = None
        self._ids: list = []
        self._query_texts: list[str] = []
        self._discriminators: list[frozenset[str]] = []
        self._manifests: list[str] = []
        self._current_manifest: str | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ load (AC-22)

    async def _ensure_loaded(self, *, settings, sessionmaker) -> None:
        """Rebuild the matrix from Postgres once per process, under the lock so concurrent first
        requests load it once rather than N times (AC-22).

        Rebuilding from Postgres at process start is what makes matrix/DB drift impossible: the
        matrix is a derived view, never the source of truth.
        """
        if self._matrix is not None:
            return
        async with self._lock:
            if self._matrix is not None:  # another coroutine won the race while we waited
                return
            self._current_manifest = await manifest_id(settings)
            async with sessionmaker() as session:
                rows = (await session.scalars(select(CacheEntry))).all()

            self._ids = [r.id for r in rows]
            self._query_texts = [r.query_text for r in rows]
            terms = frozenset(settings.CACHE_DISCRIMINATIVE_TERMS)
            self._discriminators = [
                keys.discriminators(r.query_text, terms) for r in rows
            ]
            self._manifests = [r.index_manifest_id for r in rows]
            vectors = [unpack_vector(r.embedding) for r in rows]
            if vectors:
                self._matrix = _l2_normalize(np.vstack(vectors).astype("float32"))
            else:
                self._matrix = np.zeros((0, settings.EMBED_DIM), dtype="float32")
            logger.info("rag.cache_loaded", entries=len(self._ids),
                        manifest=self._current_manifest)

    # ------------------------------------------------------------------ lookup (AC-6..AC-10)

    async def lookup(
        self, normalized: str, vec: list[float], *, settings, sessionmaker
    ) -> tuple[AnswerResponse, float] | None:
        """The cached answer + its cosine, or None on any miss.

        Returns None — never raises — for every failure mode: a backend error here must cost a
        cache hit, not the request (AC-10).
        """
        try:
            return await self._lookup(normalized, vec, settings=settings,
                                      sessionmaker=sessionmaker)
        except Exception as exc:  # noqa: BLE001 — fail-open: the cache is never a failure source
            logger.warning("rag.cache_degraded", tier="semantic", op="lookup", error=str(exc))
            return None

    async def _lookup(self, normalized, vec, *, settings, sessionmaker):
        await self._ensure_loaded(settings=settings, sessionmaker=sessionmaker)
        if self._matrix is None or len(self._ids) == 0:
            return None  # cold cache: skip the matmul entirely

        q = np.asarray(vec, dtype="float32")
        norm = np.linalg.norm(q)
        if norm == 0:
            return None
        # INLINE pure-CPU: (n, 1536) @ (1536,) -> (n,). The matrix rows are already L2-normalized,
        # so this dot product IS cosine similarity.
        sims = self._matrix @ (q / norm)
        best = int(np.argmax(sims))
        cosine = float(sims[best])

        if cosine < settings.CACHE_SIMILARITY_THRESHOLD:
            return None

        # Signal 2: the discriminative veto — what actually separates the sets. Cosine cannot do it
        # alone: the worst adversarial pair (15(3) vs 15(4), 0.930) outscores the best true
        # paraphrase (0.912). See keys.discriminators and T7's calibration.
        terms = frozenset(settings.CACHE_DISCRIMINATIVE_TERMS)
        query_disc = keys.discriminators(normalized, terms)
        if query_disc != self._discriminators[best]:
            logger.info("rag.cache_lexical_reject", cosine=cosine,
                        query_discriminators=sorted(query_disc),
                        candidate_discriminators=sorted(self._discriminators[best]),
                        candidate=self._query_texts[best])
            return None

        if self._manifests[best] != self._current_manifest:
            # Lazy expiry (AC-9): the answer quotes an index we no longer have.
            logger.info("rag.cache_stale_manifest", entry_manifest=self._manifests[best],
                        current_manifest=self._current_manifest)
            await self._evict(self._ids[best], settings=settings, sessionmaker=sessionmaker)
            return None

        entry_id = self._ids[best]
        async with sessionmaker() as session:
            row = await session.get(CacheEntry, entry_id)
            if row is None:  # deleted underneath us (--flush, poison control)
                return None
            payload = row.answer
            row.hits += 1
            row.last_hit_at = datetime.now(UTC)
            await session.commit()

        return AnswerResponse.model_validate(payload), cosine

    # ------------------------------------------------------------------ write (AC-17/18/19)

    async def write(
        self, normalized: str, vec: list[float], response: AnswerResponse, *, settings,
        sessionmaker,
    ) -> None:
        """Upsert one entry into Postgres + Redis and append it to the matrix.

        Runs write-behind (see `schedule_write`), so it is off the response path entirely — nothing
        here is allowed to be fast, and nothing here is allowed to raise into the caller.
        """
        await self._ensure_loaded(settings=settings, sessionmaker=sessionmaker)
        query_hash = keys.exact_key(normalized)
        payload = response.model_dump()

        async with self._lock:
            async with sessionmaker() as session:
                existing = await session.scalar(
                    select(CacheEntry).where(CacheEntry.query_hash == query_hash)
                )
                # Capacity is enforced before an INSERT (an update replaces a row in place and
                # cannot grow the matrix), so it can never exceed the size the
                # brute-force-is-fine argument depends on (AC-18).
                if existing is None and len(self._ids) >= settings.CACHE_MAX_ENTRIES:
                    await self._evict_least_recently_hit(
                        settings=settings, sessionmaker=sessionmaker
                    )
                if existing is not None:
                    # AC-17: re-asking a cached question updates in place. Without this the unique
                    # constraint would raise, and without the constraint the matrix would grow a
                    # duplicate row per repeat.
                    existing.query_text = normalized
                    existing.embedding = pack_vector(vec)
                    existing.answer = payload
                    existing.index_manifest_id = self._current_manifest
                    entry_id = existing.id
                else:
                    row = CacheEntry(
                        query_hash=query_hash,
                        query_text=normalized,
                        embedding=pack_vector(vec),
                        answer=payload,
                        index_manifest_id=self._current_manifest,
                        hits=0,
                    )
                    session.add(row)
                    await session.flush()
                    entry_id = row.id
                await session.commit()

            self._drop_row(entry_id)  # no-op for a fresh insert; replaces the row on an update
            self._append_row(entry_id, normalized, vec, self._current_manifest,
                             frozenset(settings.CACHE_DISCRIMINATIVE_TERMS))

        await redis_hot.set(
            f"{settings.CACHE_KEY_PREFIX}{query_hash}", payload, settings=settings
        )

    def _append_row(self, entry_id, normalized: str, vec: list[float], manifest: str,
                    discriminative_terms: frozenset[str]) -> None:
        self._ids.append(entry_id)
        self._query_texts.append(normalized)
        self._discriminators.append(keys.discriminators(normalized, discriminative_terms))
        self._manifests.append(manifest)
        row = _l2_normalize(np.asarray([vec], dtype="float32"))
        self._matrix = row if self._matrix is None or not len(self._matrix) else np.vstack(
            [self._matrix, row]
        )

    async def _evict_least_recently_hit(self, *, settings, sessionmaker) -> None:
        """At capacity, drop the coldest entry: never hit, or hit longest ago. `created_at` breaks
        the tie so a never-hit entry is judged on its age rather than arbitrarily."""
        async with sessionmaker() as session:
            victim = await session.scalar(
                select(CacheEntry).order_by(
                    CacheEntry.last_hit_at.asc().nullsfirst(), CacheEntry.created_at.asc()
                ).limit(1)
            )
            if victim is None:
                return
            victim_id, victim_hash = victim.id, victim.query_hash
            await redis_hot.delete(
                f"{settings.CACHE_KEY_PREFIX}{victim_hash}", settings=settings
            )
            await session.delete(victim)
            await session.commit()
        self._drop_row(victim_id)
        logger.info("rag.cache_evicted", reason="capacity", entries=len(self._ids))

    # ------------------------------------------------------------------ ops (AC-20/AC-21)

    async def flush(self, *, settings, sessionmaker) -> int:
        """Delete every entry from both tiers. Returns the Postgres rows deleted."""
        async with sessionmaker() as session:
            count = await session.scalar(select(func.count()).select_from(CacheEntry)) or 0
            await session.execute(sa_delete(CacheEntry))
            await session.commit()
        await redis_hot.flush(settings=settings)
        await self.reset()
        return int(count)

    async def delete_by_query(self, query: str, *, settings, sessionmaker) -> int:
        """Poison control: drop the single entry a question maps to (AC-21). Returns rows deleted.

        Keyed on the query rather than a request id because nothing generates a request id yet
        (F13 owns request logging) — and because the question is what an operator actually has in
        hand when they spot a bad answer.
        """
        query_hash = keys.exact_key(keys.normalize(query))
        async with sessionmaker() as session:
            row = await session.scalar(
                select(CacheEntry).where(CacheEntry.query_hash == query_hash)
            )
            if row is None:
                return 0
            entry_id = row.id
            await session.delete(row)
            await session.commit()
        await redis_hot.delete(f"{settings.CACHE_KEY_PREFIX}{query_hash}", settings=settings)
        self._drop_row(entry_id)
        return 1

    # ------------------------------------------------------------------ mutation helpers

    async def _evict(self, entry_id, *, settings, sessionmaker) -> None:
        """Drop one entry from Postgres, Redis and the matrix — all three, or the tiers disagree."""
        async with sessionmaker() as session:
            row = await session.get(CacheEntry, entry_id)
            if row is not None:
                # The Redis key is the prefix + the SAME hash Postgres stores, so the two tiers
                # cannot disagree about which key an entry owns.
                await redis_hot.delete(
                    f"{settings.CACHE_KEY_PREFIX}{row.query_hash}", settings=settings
                )
                await session.delete(row)
                await session.commit()
        self._drop_row(entry_id)

    def _drop_row(self, entry_id) -> None:
        """Remove one row from the parallel arrays + matrix. Callers hold `_lock` or are already
        serialized by the event loop between awaits."""
        try:
            i = self._ids.index(entry_id)
        except ValueError:
            return
        for arr in (self._ids, self._query_texts, self._discriminators, self._manifests):
            del arr[i]
        if self._matrix is not None and len(self._matrix):
            self._matrix = np.delete(self._matrix, i, axis=0)

    async def reset(self) -> None:
        """Drop the loaded matrix so the next lookup rebuilds from Postgres. Used by tests (the
        singleton would otherwise leak entries across cases) and by `--flush`."""
        async with self._lock:
            self._matrix = None
            self._ids = []
            self._query_texts = []
            self._discriminators = []
            self._manifests = []
            self._current_manifest = None


_CACHE = SemanticCache()

# asyncio only holds a WEAK reference to a running task. Without a strong reference here, a
# write-behind task can be garbage-collected mid-await and the entry silently never lands — the
# canonical create_task footgun, and one that would present as a mysteriously low hit rate at the
# eval gate rather than as an error.
_WRITE_TASKS: set[asyncio.Task] = set()


async def lookup(normalized: str, vec: list[float], *, settings, sessionmaker):
    return await _CACHE.lookup(normalized, vec, settings=settings, sessionmaker=sessionmaker)


async def _write_guarded(normalized, vec, response, *, settings, sessionmaker) -> None:
    try:
        await _CACHE.write(normalized, vec, response, settings=settings, sessionmaker=sessionmaker)
    except Exception as exc:  # noqa: BLE001 — a failed cache write must never surface (AC-19)
        logger.warning("rag.cache_write_failed", tier="semantic", error=str(exc))


def schedule_write(
    normalized: str, vec: list[float], response: AnswerResponse, *, settings, sessionmaker
) -> asyncio.Task:
    """Fire-and-forget the write (AC-14/AC-15).

    Called AFTER the terminal `done` event, so the cache write can never add latency to the
    response path — that is the entire reason this is write-behind rather than a plain await.
    Returns the task so tests can drain it deterministically; callers ignore it.
    """
    task = asyncio.create_task(
        _write_guarded(normalized, vec, response, settings=settings, sessionmaker=sessionmaker)
    )
    _WRITE_TASKS.add(task)
    task.add_done_callback(_WRITE_TASKS.discard)
    return task


async def write(normalized: str, vec: list[float], response: AnswerResponse, *, settings,
                sessionmaker) -> None:
    await _CACHE.write(normalized, vec, response, settings=settings, sessionmaker=sessionmaker)


async def flush(*, settings, sessionmaker) -> int:
    return await _CACHE.flush(settings=settings, sessionmaker=sessionmaker)


async def delete_by_query(query: str, *, settings, sessionmaker) -> int:
    return await _CACHE.delete_by_query(query, settings=settings, sessionmaker=sessionmaker)


async def drain_writes() -> None:
    """Await every in-flight write-behind task. For tests and for F11's shutdown hook — prod never
    calls this on the request path."""
    if _WRITE_TASKS:
        await asyncio.gather(*list(_WRITE_TASKS), return_exceptions=True)


async def reset() -> None:
    await _CACHE.reset()
