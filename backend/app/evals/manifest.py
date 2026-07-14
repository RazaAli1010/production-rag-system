"""Run provenance (T3, AC-23).

Every run records the git SHA + the index manifest so a recorded number is reproducible (the
CLAUDE.md eval-gate label -> SHA -> manifest chain). `git_sha` shells out asynchronously (never a
blocking `subprocess.run` on the loop); a missing/failed git call degrades to `"unknown"` + a
warning rather than aborting the whole eval run. `index_manifest_snapshot` reuses F2's
`read_manifest` verbatim.
"""

import asyncio

import structlog

from app.indexing.manifest import read_manifest

logger = structlog.get_logger(__name__)


async def git_sha() -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "rev-parse", "HEAD",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.warning("evals.git_sha_failed", returncode=proc.returncode,
                           stderr=stderr.decode(errors="replace").strip())
            return "unknown"
        return stdout.decode().strip() or "unknown"
    except (OSError, ValueError) as exc:  # git binary absent, etc.
        logger.warning("evals.git_sha_error", error=str(exc))
        return "unknown"


async def index_manifest_snapshot(settings) -> dict:
    manifest = await read_manifest(settings)
    return manifest.model_dump() if manifest is not None else {}
