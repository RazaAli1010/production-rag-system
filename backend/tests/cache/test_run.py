"""F9 CLI: --flush / --delete-query. Mirrors `tests/evals/test_run.py`'s injected-settings style."""

import pytest
from sqlalchemy import func, select

from app.caching import run as cache_run
from app.caching import store
from app.db.models.ops import CacheEntry
from tests.cache.conftest import make_answer, unit


async def _count(sessionmaker_) -> int:
    async with sessionmaker_() as s:
        return int(await s.scalar(select(func.count()).select_from(CacheEntry)) or 0)


@pytest.fixture
def seeded(settings_with_manifest, sessionmaker_):
    async def _seed():
        await store.write("fee refund policy", unit(1.0), make_answer(),
                          settings=settings_with_manifest, sessionmaker=sessionmaker_)
        await store.write("probation rules", unit(0.0, 1.0), make_answer(),
                          settings=settings_with_manifest, sessionmaker=sessionmaker_)

    return _seed


# --------------------------------------------------------------- --flush (AC-20)

async def test_flush_empties_the_cache_and_exits_zero(seeded, settings_with_manifest,
                                                      sessionmaker_, capsys):
    await seeded()
    assert await _count(sessionmaker_) == 2

    code = await cache_run.main(["--flush"], settings=settings_with_manifest,
                                sessionmaker=sessionmaker_)

    assert code == 0
    assert await _count(sessionmaker_) == 0
    assert "flushed 2" in capsys.readouterr().out


async def test_flush_on_an_empty_cache_is_still_success(settings_with_manifest, sessionmaker_):
    assert await cache_run.main(["--flush"], settings=settings_with_manifest,
                                sessionmaker=sessionmaker_) == 0


async def test_flush_succeeds_when_redis_is_down(seeded, settings_with_manifest, sessionmaker_,
                                                 monkeypatch):
    """Redis being unreachable must not stop --flush from clearing Postgres — otherwise a Redis
    outage would leave an operator with no way to clear a poisoned cache at all.

    The failure is injected at the CLIENT, not by replacing `redis_hot.flush`: replacing the
    function would step over the very fail-open handling this test exists to prove.
    """
    class DeadRedis:
        async def scan_iter(self, match=None):
            raise ConnectionError("redis is down")
            yield  # pragma: no cover — makes this an async generator

        async def delete(self, *keys):
            raise ConnectionError("redis is down")

    monkeypatch.setattr(store.redis_hot, "_client", lambda settings: DeadRedis())
    await seeded()

    code = await cache_run.main(["--flush"],
                                settings=settings_with_manifest.model_copy(
                                    update={"REDIS_URL": "redis://localhost:6379/0"}),
                                sessionmaker=sessionmaker_)

    assert code == 0
    assert await _count(sessionmaker_) == 0, "Postgres must still be cleared"


# --------------------------------------------------------------- --delete-query (AC-21)

async def test_delete_query_removes_one_entry(seeded, settings_with_manifest, sessionmaker_,
                                              capsys):
    await seeded()

    code = await cache_run.main(["--delete-query", "fee refund policy"],
                                settings=settings_with_manifest, sessionmaker=sessionmaker_)

    assert code == 0
    assert await _count(sessionmaker_) == 1
    assert "deleted 1 entry" in capsys.readouterr().out


async def test_delete_query_exits_one_when_nothing_matched(seeded, settings_with_manifest,
                                                           sessionmaker_):
    await seeded()

    code = await cache_run.main(["--delete-query", "never asked this"],
                                settings=settings_with_manifest, sessionmaker=sessionmaker_)

    assert code == 1
    assert await _count(sessionmaker_) == 2


async def test_delete_query_normalizes_the_operators_paste(seeded, settings_with_manifest,
                                                           sessionmaker_):
    """The operator pastes what the student typed, not a pre-normalized key."""
    await seeded()

    code = await cache_run.main(["--delete-query", "  Fee Refund Policy?  "],
                                settings=settings_with_manifest, sessionmaker=sessionmaker_)

    assert code == 0
    assert await _count(sessionmaker_) == 1


# --------------------------------------------------------------- usage

async def test_no_args_is_a_usage_error(settings_with_manifest, sessionmaker_):
    assert await cache_run.main([], settings=settings_with_manifest,
                                sessionmaker=sessionmaker_) == 2


async def test_both_flags_is_a_usage_error(settings_with_manifest, sessionmaker_):
    code = await cache_run.main(["--flush", "--delete-query", "x"],
                                settings=settings_with_manifest, sessionmaker=sessionmaker_)
    assert code == 2
