"""T14 / AC-35: F17 adds no schema. F12's 0001_initial already shipped every `sessions`/`messages`
column the feature needs (the over-budget marker and pending-pair count are DERIVED, not stored), so
an autogenerate diff after `upgrade head` must be empty. This guard stops the zero-migration claim
from silently rotting into a stray column.
"""

import subprocess
import sys
from pathlib import Path

from sqlalchemy import inspect

BACKEND_DIR = Path(__file__).resolve().parents[2]

MEMORY_TABLES = {"sessions", "messages"}


def _run_alembic(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND_DIR, capture_output=True, text=True,
    )


def test_autogenerate_after_f17_is_empty():
    assert _run_alembic("upgrade", "head").returncode == 0
    result = _run_alembic("check")
    assert result.returncode == 0, (
        "alembic check found pending schema operations — F17 must add none.\n"
        f"{result.stdout}\n{result.stderr}"
    )


async def test_sessions_messages_have_the_columns_f17_uses(engine):
    """Every column F17 reads/writes already exists — the reason no migration is needed."""

    def _columns(sync_conn, table):
        return {c["name"] for c in inspect(sync_conn).get_columns(table)}

    async with engine.connect() as conn:
        session_cols = await conn.run_sync(_columns, "sessions")
        message_cols = await conn.run_sync(_columns, "messages")

    assert {"total_tokens", "summary", "summary_token_count", "summarized_upto_message_id",
            "last_active_at", "title", "is_archived"} <= session_cols
    assert {"role", "content", "token_count", "citations", "refused", "created_at"} <= message_cols


async def test_no_pending_or_needs_summarize_column(engine):
    """The trigger state is derived, not stored — asserting the columns we deliberately did NOT add."""

    def _columns(sync_conn):
        return {c["name"] for c in inspect(sync_conn).get_columns("sessions")}

    async with engine.connect() as conn:
        cols = await conn.run_sync(_columns)

    assert "needs_summarize" not in cols
    assert "pending_pairs" not in cols
