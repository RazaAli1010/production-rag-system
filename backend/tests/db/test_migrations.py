"""T-11: alembic upgrade head builds all 12 tables/enums/indexes; downgrade drops cleanly;
autogenerate against the upgraded schema produces an empty diff (schema matches models)."""

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


@pytest.mark.asyncio
async def test_upgrade_head_creates_all_tables(engine):
    result = _run_alembic("upgrade", "head")
    assert result.returncode == 0, result.stderr

    async with engine.connect() as conn:
        def _inspect(sync_conn):
            return set(inspect(sync_conn).get_table_names())

        tables = await conn.run_sync(_inspect)

    expected = {
        "users", "api_keys", "refresh_tokens", "login_attempts", "documents", "chunks",
        "sessions", "messages", "request_logs", "cache_entries", "eval_runs", "eval_results",
        "alembic_version",
    }
    assert expected <= tables


@pytest.mark.asyncio
async def test_autogenerate_empty_diff_after_upgrade():
    # Ensure we're at head, then ask alembic to autogenerate — it should propose no changes.
    up = _run_alembic("upgrade", "head")
    assert up.returncode == 0, up.stderr

    result = _run_alembic("check")
    assert result.returncode == 0, (
        f"alembic detected drift between models and migrations:\n{result.stdout}\n{result.stderr}"
    )


@pytest.mark.asyncio
async def test_downgrade_base_drops_everything(engine):
    up = _run_alembic("upgrade", "head")
    assert up.returncode == 0, up.stderr

    down = _run_alembic("downgrade", "base")
    assert down.returncode == 0, down.stderr

    async with engine.connect() as conn:
        def _inspect(sync_conn):
            return set(inspect(sync_conn).get_table_names())

        tables = await conn.run_sync(_inspect)

    app_tables = {
        "users", "api_keys", "refresh_tokens", "login_attempts", "documents", "chunks",
        "sessions", "messages", "request_logs", "cache_entries", "eval_runs", "eval_results",
    }
    assert app_tables.isdisjoint(tables)

    # Leave the DB back at head for any other test in the session relying on schema presence.
    redo = _run_alembic("upgrade", "head")
    assert redo.returncode == 0, redo.stderr
