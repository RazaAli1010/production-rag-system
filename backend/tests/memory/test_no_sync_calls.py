"""In-suite async grep-guard for app/memory (mirrors the `memory:` CI job).

Bans the sync twins forbidden by the CLAUDE.md async mandate: `.invoke(`, `.embed_query(`/
`.embed_documents(`, blocking `.stream(`, `import requests`, bare sync `redis`, `create_engine(`.
The summariser is the only OpenAI call and must go through `ainvoke`; tiktoken counting is cheap
pure-CPU and runs inline (no thread offload appears here and none should).
"""

import re
from pathlib import Path

MEMORY_DIR = Path(__file__).resolve().parents[2] / "app" / "memory"


def test_no_sync_call_sites_in_app_memory():
    violations = []
    for path in MEMORY_DIR.glob("*.py"):
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
    assert violations == [], f"sync call sites found in app/memory/: {violations}"


def test_summariser_uses_ainvoke():
    """Guard against the guard passing vacuously: the summariser must actually call the async
    surface, not skip the LLM entirely."""
    text = (MEMORY_DIR / "summarizer.py").read_text(encoding="utf-8")
    assert ".ainvoke(" in text
