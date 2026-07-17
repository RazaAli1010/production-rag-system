"""In-suite async grep-guard for app/caching (mirrors the `caching:` CI job + tests/evals' guard).

Bans the sync twins forbidden by the CLAUDE.md async mandate. This module matters more than the
other packages' guards: `app/caching` is the ONLY module in the app that talks to Redis, and the
sync `redis` client is both the obvious import to reach for and a silent event-loop blocker.

The cosine matmul, sha256 and set math in `keys`/`store` are cheap pure-CPU and deliberately run
inline (CLAUDE.md names "cosine matmul on the cache matrix" as the inline side of the line), so no
`anyio.to_thread` offload appears here and none should.
"""

import re
from pathlib import Path

CACHING_DIR = Path(__file__).resolve().parents[2] / "app" / "caching"


def test_no_sync_call_sites_in_app_caching():
    violations = []
    for path in CACHING_DIR.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if re.search(r"\.invoke\(", line) or re.search(r"\.embed_query\(", line):
                violations.append(f"{path.name}:{lineno}: {line.strip()}")
            if re.search(r"\.embed_documents\(", line):
                violations.append(f"{path.name}:{lineno}: {line.strip()}")
            if re.search(r"(?<!a)\.stream\(", line):  # allow .astream(/.astream_events(
                violations.append(f"{path.name}:{lineno}: {line.strip()}")
            if re.search(r"^\s*import requests\b", line) or re.search(r"create_engine\(", line):
                violations.append(f"{path.name}:{lineno}: {line.strip()}")
    assert violations == [], f"sync call sites found in app/caching/: {violations}"


def test_no_sync_redis_import_in_app_caching():
    """The sync client is the whole point of this file: `import redis` gives you a blocking client
    whose calls look identical to the async one's at the call site."""
    violations = []
    for path in CACHING_DIR.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if re.match(r"^\s*import redis\s*$", line):
                violations.append(f"{path.name}:{lineno}: {line.strip()}")
            if re.match(r"^\s*from redis import", line):
                violations.append(f"{path.name}:{lineno}: {line.strip()}")
    assert violations == [], (
        f"sync redis import found in app/caching/ (use 'import redis.asyncio as redis'): "
        f"{violations}"
    )


def test_redis_is_actually_imported_the_async_way():
    """Guards against the guard passing vacuously — if redis_hot ever stopped importing redis at
    all, both tests above would go green while the hot tier quietly did nothing."""
    text = (CACHING_DIR / "redis_hot.py").read_text(encoding="utf-8")
    assert "import redis.asyncio as redis" in text
