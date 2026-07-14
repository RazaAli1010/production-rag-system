"""In-suite async grep-guard for app/evals (mirrors the CI job + tests/rag's guard).

Bans the sync twins forbidden by the CLAUDE.md async mandate. RAGAS's blocking `evaluate()` is the
one allowed CPU/IO offload — reached only via `anyio.to_thread.run_sync`, never called directly on
the loop, so no sync invoke/stream call site appears here either.
"""

import re
from pathlib import Path

EVALS_DIR = Path(__file__).resolve().parents[2] / "app" / "evals"


def test_no_sync_call_sites_in_app_evals():
    violations = []
    for path in EVALS_DIR.glob("*.py"):
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
    assert violations == [], f"sync call sites found in app/evals/: {violations}"
