"""AC-46: F10 adds no schema. F12's 0001_initial already shipped every column the feature needs, so
an autogenerate diff after `upgrade head` must be empty. This is the guard that stops the
zero-migration claim from silently rotting.
"""

import subprocess
import sys
from pathlib import Path

from sqlalchemy import inspect

BACKEND_DIR = Path(__file__).resolve().parents[2]

AUTH_TABLES = {"users", "api_keys", "refresh_tokens", "login_attempts"}


def _run_alembic(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND_DIR,
        capture_output=True,
        text=True,
    )


def test_head_is_still_0003():
    """If a later migration appears, F10 (or something after it) changed the schema — the specs
    say it must not."""
    result = _run_alembic("heads")

    assert result.returncode == 0, result.stderr
    assert "0003" in result.stdout


def test_autogenerate_after_f10_is_empty():
    assert _run_alembic("upgrade", "head").returncode == 0

    result = _run_alembic("check")

    assert result.returncode == 0, (
        "alembic check found pending schema operations — F10 must add none.\n"
        f"{result.stdout}\n{result.stderr}"
    )


async def test_f10_reads_only_tables_f12_already_created(engine):
    def _tables(sync_conn):
        return set(inspect(sync_conn).get_table_names())

    async with engine.connect() as conn:
        tables = await conn.run_sync(_tables)

    assert AUTH_TABLES <= tables


async def test_api_keys_has_no_scope_column(engine):
    """One scope (ask-only) is a constant, not a column — the decision that keeps F10 at zero
    migrations."""

    def _columns(sync_conn):
        return {c["name"] for c in inspect(sync_conn).get_columns("api_keys")}

    async with engine.connect() as conn:
        columns = await conn.run_sync(_columns)

    assert "scope" not in columns
    assert {"key_hash", "label", "revoked_at", "user_id"} <= columns
