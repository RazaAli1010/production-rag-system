"""T2: `alembic upgrade head` then `downgrade -1` runs clean; `cache_entries.query_hash` and its
unique constraint are present after upgrade and absent after downgrade. Mirrors
`tests/ingestion/test_migration_0002.py`'s subprocess pattern.
"""

import subprocess
import sys
from pathlib import Path

from sqlalchemy import inspect

BACKEND_DIR = Path(__file__).resolve().parents[2]


def _run_alembic(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND_DIR,
        capture_output=True,
        text=True,
    )


def _cache_columns(sync_conn) -> dict:
    return {c["name"]: c for c in inspect(sync_conn).get_columns("cache_entries")}


def _cache_unique_constraints(sync_conn) -> set:
    return {u["name"] for u in inspect(sync_conn).get_unique_constraints("cache_entries")}


async def test_upgrade_head_adds_query_hash(engine):
    result = _run_alembic("upgrade", "head")
    assert result.returncode == 0, result.stderr

    async with engine.connect() as conn:
        columns = await conn.run_sync(_cache_columns)
        uniques = await conn.run_sync(_cache_unique_constraints)

    assert "query_hash" in columns
    assert columns["query_hash"]["nullable"] is False
    # The server_default must be dropped after backfill, or `alembic check` reports drift against
    # the model (which declares no default).
    assert columns["query_hash"]["default"] is None
    assert "uq_cache_entries_query_hash" in uniques


async def test_upgrade_does_not_touch_f12_columns(engine):
    """0001 owns this table; 0003 adds one column and must recreate nothing. `embedding` staying
    LargeBinary is the load-bearing assertion — it is what keeps pgvector out."""
    result = _run_alembic("upgrade", "head")
    assert result.returncode == 0, result.stderr

    async with engine.connect() as conn:
        columns = await conn.run_sync(_cache_columns)

    for name in ("id", "query_text", "embedding", "answer", "index_manifest_id", "hits",
                 "created_at", "last_hit_at"):
        assert name in columns, f"0003 dropped or renamed F12's {name}"
    assert "BYTEA" in str(columns["embedding"]["type"]).upper()
    assert "request_id" not in columns  # deliberately deferred to F13 — see the migration docstring


async def test_downgrade_one_removes_query_hash(engine):
    up = _run_alembic("upgrade", "head")
    assert up.returncode == 0, up.stderr

    # By name, not `-1`: `-1` only means "undo 0003" while 0003 is head, so this would quietly stop
    # testing 0003 the moment a 0004 lands (exactly how F9 broke test_migration_0002's `-1`).
    down = _run_alembic("downgrade", "0002")
    assert down.returncode == 0, down.stderr

    async with engine.connect() as conn:
        columns = await conn.run_sync(_cache_columns)
        uniques = await conn.run_sync(_cache_unique_constraints)

    assert "query_hash" not in columns
    assert "uq_cache_entries_query_hash" not in uniques

    # leave head restored for any other test relying on schema presence
    redo = _run_alembic("upgrade", "head")
    assert redo.returncode == 0, redo.stderr


def test_alembic_check_empty_diff_after_upgrade():
    up = _run_alembic("upgrade", "head")
    assert up.returncode == 0, up.stderr

    result = _run_alembic("check")
    assert result.returncode == 0, (
        f"alembic detected drift between models and migrations:\n{result.stdout}\n{result.stderr}"
    )
