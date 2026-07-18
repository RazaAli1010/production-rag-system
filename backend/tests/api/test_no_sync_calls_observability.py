"""In-suite async grep-guard for app/observability (mirrors the memory/cache CI jobs, AC-12).

The request_logs write and the stats aggregates all use the async SQLAlchemy session; nothing here
may reach for a sync twin (`create_engine(`, blocking `requests`, sync `redis`, `.invoke(`,
`.embed_*(`, blocking `.stream(`).
"""

import re
from pathlib import Path

OBS_DIR = Path(__file__).resolve().parents[2] / "app" / "observability"


def test_no_sync_call_sites_in_app_observability():
    violations = []
    for path in OBS_DIR.glob("*.py"):
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
            if re.match(r"^\s*import redis\s*$", line) or re.match(r"^\s*from redis import", line):
                violations.append(f"{path.name}:{lineno}: {line.strip()}")
    assert violations == [], f"sync call sites found in app/observability/: {violations}"
