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
