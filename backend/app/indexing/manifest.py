import hashlib

import aiofiles

from app.indexing.schemas import Manifest


async def write_manifest(m, settings):
    path = settings.INDEX_MANIFEST_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    async with aiofiles.open(path, "w", encoding="utf-8") as f:
        await f.write(m.model_dump_json(indent=2))
    return path


async def read_manifest(settings):
    path = settings.INDEX_MANIFEST_PATH
    if not path.exists():
        return None
    async with aiofiles.open(path, encoding="utf-8") as f:
        return Manifest.model_validate_json(await f.read())


async def manifest_id(settings) -> str:
    """A stable id for the current index — what F9 stores in `cache_entries.index_manifest_id` and
    compares on lookup to expire answers that quote a corpus we no longer have (F9 AC-9).

    `Manifest` carries no id field, so this is a content hash of the manifest itself: it changes iff
    the index changes (strategy, embed model, per-namespace counts, tokens, `created_at`), which is
    exactly the invalidation signal. `"none"` when no manifest exists — a cache populated before the
    first index is entirely stale by definition, so every entry then fails the comparison.
    """
    m = await read_manifest(settings)
    if m is None:
        return "none"
    return hashlib.sha256(m.model_dump_json().encode("utf-8")).hexdigest()[:16]


async def guard_strategy(requested, wipe, settings):
    if wipe:
        return
    existing = await read_manifest(settings)
    if existing is None:
        return
    if existing.strategy != requested:
        raise SystemExit(
            f"strategy drift: manifest={existing.strategy} requested={requested}; pass --wipe"
        )
