"""Session-wide test setup.

Three conftests (`tests/rag`, `tests/indexing`, `tests/ingestion`) TRUNCATE `documents`/`chunks`
on teardown. Every conftest builds its engine from `settings.DATABASE_URL`, so running the suite
against the dev database wipes the ingested corpus — every subsequent answer refuses, and the only
way back is a full re-ingest + reindex. That is the recorded cause of `false_refusal_rate=1.0` in
the f7/f8 eval reports.

Fixed once, here, rather than in each truncating conftest: the whole suite is redirected to a
dedicated `<dev-db>_pytest` database, created and migrated on first use. An environment variable
outranks the `.env` file in pydantic-settings, and this module is imported before any test module
imports `app.core.settings`, so no other file needs to change — including conftests added later.
"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

BACKEND_DIR = Path(__file__).resolve().parents[1]
TEST_DB_SUFFIX = "_pytest"

# Settings.JWT_SECRET is required with no default (a defaulted signing key would ship as a
# vulnerability), and `settings = Settings()` runs at import. Every suite therefore needs one in the
# environment before any test module imports app.core.settings. setdefault, so a real CI/dev value
# still wins.
os.environ.setdefault("JWT_SECRET", "test-only-not-a-real-secret")


def _configured_url() -> str:
    """The URL the app would use: env first, then `.env` — the same precedence Settings applies."""
    if url := os.environ.get("DATABASE_URL"):
        return url
    from dotenv import dotenv_values

    return dotenv_values(BACKEND_DIR / ".env").get("DATABASE_URL") or ""


def _sibling_test_url(url: str) -> str:
    parts = urlsplit(url)
    name = parts.path.lstrip("/")
    if not name or name.endswith(TEST_DB_SUFFIX):
        return url  # already a test DB — leave it alone (CI may point straight at one)
    return urlunsplit(parts._replace(path=f"/{name}{TEST_DB_SUFFIX}"))


async def _create_database_if_missing(url: str) -> None:
    import asyncpg

    # asyncpg takes a plain libpq DSN, not SQLAlchemy's "+asyncpg" dialect form. CREATE DATABASE
    # cannot run inside a transaction or against the target DB itself, hence the "postgres" hop.
    parts = urlsplit(url.replace("+asyncpg", ""))
    name = parts.path.lstrip("/")
    conn = await asyncpg.connect(urlunsplit(parts._replace(path="/postgres")))
    try:
        if not await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", name):
            await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()


_DEV_URL = _configured_url()
_TEST_URL = _sibling_test_url(_DEV_URL)
if _TEST_URL and _TEST_URL != _DEV_URL:
    os.environ["DATABASE_URL"] = _TEST_URL


def pytest_configure(config):
    """Create + migrate the test database before collection (env var is already set at import)."""
    if not _TEST_URL or _TEST_URL == _DEV_URL:
        return
    asyncio.run(_create_database_if_missing(_TEST_URL))
    # Subprocess, not `alembic.command`: alembic's env.py imports app settings and calls
    # asyncio.run itself — running it in-process would bind the Settings singleton mid-collection.
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND_DIR, env=os.environ, check=True, capture_output=True,
    )
