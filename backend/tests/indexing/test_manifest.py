import pytest

from app.core.settings import Settings
from app.indexing.manifest import guard_strategy, read_manifest, write_manifest
from app.indexing.schemas import Manifest


def _settings(tmp_path):
    return Settings(_env_file=None, DATABASE_URL="postgresql+asyncpg://u:p@localhost:5432/db",
                    ADMIN_EMAIL="a@b.c", ADMIN_PASSWORD="x", OPENAI_API_KEY="k",
                    PINECONE_API_KEY="k", PINECONE_INDEX="i",
                    INDEX_MANIFEST_PATH=tmp_path / "m.json")


def _manifest(strategy="fixed"):
    return Manifest(strategy=strategy, embed_model="text-embedding-3-small",
                    namespaces={"pu": {"vectors": 3, "chunks": 3}}, total_tokens=9,
                    est_cost_usd=0.0001, created_at="2026-07-13T00:00:00Z")


async def test_write_then_read_roundtrip(tmp_path):
    s = _settings(tmp_path)
    await write_manifest(_manifest(), s)
    got = await read_manifest(s)
    assert got == _manifest()


async def test_read_missing_returns_none(tmp_path):
    assert await read_manifest(_settings(tmp_path)) is None


async def test_guard_no_manifest_ok(tmp_path):
    await guard_strategy("structure", False, _settings(tmp_path))


async def test_guard_drift_without_wipe_aborts(tmp_path):
    s = _settings(tmp_path)
    await write_manifest(_manifest("fixed"), s)
    with pytest.raises(SystemExit):
        await guard_strategy("structure", False, s)


async def test_guard_drift_with_wipe_ok(tmp_path):
    s = _settings(tmp_path)
    await write_manifest(_manifest("fixed"), s)
    await guard_strategy("structure", True, s)
