"""T3 — run provenance (git SHA + index manifest snapshot)."""

import re

from app.evals import manifest as manifest_mod
from tests.evals.conftest import make_settings


async def test_git_sha_returns_hex_in_repo():
    sha = await manifest_mod.git_sha()
    assert sha == "unknown" or re.fullmatch(r"[0-9a-f]{40}", sha)


async def test_git_sha_unknown_on_failure(monkeypatch):
    async def boom(*a, **k):
        raise OSError("git not found")

    monkeypatch.setattr(manifest_mod.asyncio, "create_subprocess_exec", boom)
    assert await manifest_mod.git_sha() == "unknown"


async def test_index_manifest_snapshot_empty_when_missing(tmp_path):
    s = make_settings(INDEX_MANIFEST_PATH=tmp_path / "nope.json")
    assert await manifest_mod.index_manifest_snapshot(s) == {}
