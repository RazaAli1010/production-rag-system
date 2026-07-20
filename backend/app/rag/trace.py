"""Per-request pipeline trace — the intermediate output each stage produced, carried out to the UI
on the `detail` field of that stage's `done` SSE frame.

This exists so the pipeline can be SEEN: what BM25 matched vs what the vector search matched, how
reranking reordered them, what compression threw away. Same job the existing out-of-band signals do
(`hybrid.was_degraded()`, `rerank.last_rerank_ms()`, `rewrite.last_rewrite()`) — read by
`stages.emit` instead of by `_pipeline_events` — so no seam signature changes to carry it.

A MUTABLE DICT in the ContextVar, not `.set()` per stage, and that is load-bearing: with query
rewrite on, `hybrid_retrieve` runs inside `asyncio.gather` child tasks
(`rewrite.multi_query_retrieve`). Each child gets a COPY of the context, so a `.set()` in there is
invisible to the parent — the reason `_DEGRADED` is silently always-False whenever rewrite is on
(pre-existing; not fixed here). Mutating a dict the parent already holds a reference to works from
any child, because the copied context still points at the same object.

Off (`ENABLE_TRACE=false`) `start()` installs nothing and every `record()` is a dict-get + return.
"""

import contextvars
from typing import Any

_TRACE: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "pipeline_trace", default=None
)

# Demo payloads ride an SSE frame that also carries live tokens, so both are capped hard.
MAX_ITEMS = 8
MAX_TEXT = 240


def start(settings) -> None:
    """Install a fresh trace for this request. Called once at the top of `_pipeline_events`."""
    _TRACE.set({} if settings.ENABLE_TRACE else None)


def record(stage: str, payload: dict) -> None:
    """Attach `payload` to `stage`. No-op when tracing is off."""
    trace = _TRACE.get()
    if trace is not None:
        trace[stage] = payload


def append(stage: str, payload: dict) -> None:
    """Append to a LIST under `stage` — for stages that genuinely run more than once per request.
    Query rewrite fans out, so `searching` happens once per fan-out query and the UI shows one card
    each; last-write-wins would show one arbitrary variant (gather order isn't deterministic)."""
    trace = _TRACE.get()
    if trace is not None:
        trace.setdefault(stage, {"runs": []})["runs"].append(payload)


def pop(stage: str) -> dict | None:
    """Read-and-remove this stage's detail, mirroring `last_rewrite()`'s read-and-reset. Removing
    matters: `searching` emits `done` once per request, so a leftover entry would otherwise
    reattach to a later stage of the same name."""
    trace = _TRACE.get()
    return trace.pop(stage, None) if trace is not None else None


def clip(text: str) -> str:
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT] + "…"


def chunk_row(chunk, score_attr: str | None = None) -> dict:
    """One chunk as the UI shows it: enough to recognise the passage, not the whole passage."""
    row = {
        "chunk_id": chunk.chunk_id,
        "title": chunk.title,
        "section": chunk.section_heading,
        "page": chunk.page_start,
        "text": clip(chunk.text),
    }
    if score_attr:
        row["score"] = getattr(chunk, score_attr, None)
    return row


def chunk_rows(chunks, score_attr: str | None = None) -> list[dict]:
    return [chunk_row(c, score_attr) for c in chunks[:MAX_ITEMS]]
