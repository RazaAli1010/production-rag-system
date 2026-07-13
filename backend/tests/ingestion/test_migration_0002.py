"""T2: `alembic upgrade head` then `downgrade -1` runs clean; the F1 columns are present after
upgrade and absent after downgrade. Mirrors `tests/db/test_migrations.py`'s subprocess pattern.
"""

import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import inspect

BACKEND_DIR = Path(__file__).resolve().parents[2]


def _run_alembic(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND_DIR,
        capture_output=True,
        text=True,
    )


def _documents_columns(sync_conn) -> dict:
    return {c["name"]: c for c in inspect(sync_conn).get_columns("documents")}


@pytest.mark.asyncio
async def test_upgrade_head_adds_f1_columns(engine):
    result = _run_alembic("upgrade", "head")
    assert result.returncode == 0, result.stderr

    async with engine.connect() as conn:
        columns = await conn.run_sync(_documents_columns)

    assert "note" in columns
    assert columns["note"]["nullable"] is True
    assert columns["sha256"]["nullable"] is True
    assert columns["downloaded_at"]["nullable"] is True

    async with engine.connect() as conn:
        def _indexes(sync_conn):
            return {ix["name"] for ix in inspect(sync_conn).get_indexes("documents")}

        indexes = await conn.run_sync(_indexes)
    assert "ix_documents_status" in indexes


@pytest.mark.asyncio
async def test_downgrade_one_removes_f1_columns(engine):
    up = _run_alembic("upgrade", "head")
    assert up.returncode == 0, up.stderr

    down = _run_alembic("downgrade", "-1")
    assert down.returncode == 0, down.stderr

    async with engine.connect() as conn:
        columns = await conn.run_sync(_documents_columns)

    assert "note" not in columns
    assert columns["sha256"]["nullable"] is False
    assert columns["downloaded_at"]["nullable"] is False

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
