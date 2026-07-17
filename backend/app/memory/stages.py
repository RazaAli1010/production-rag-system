"""The single F17 stage emitter (CLAUDE.md: "each seam emits paired stage events via the single F17
emitter app/memory/stages.py"). Wraps `rag.events.stage_event` so F3–F9 and the ask route share one
vocabulary and Langfuse spans (F13) can derive from the same call.

Stage events are yielded INLINE by the pipeline's async generator (`baseline._pipeline_events`) and
by the ask route, interleaving before `token` events on the one SSE stream (AC-29). A slow client
backpressures at the `StreamingResponse` boundary — which throttles stages and tokens together and
in order.

ponytail: no separate fire-and-forget drop-queue. The design floated one "so a slow client never
stalls generation", but the inline generator already gives ordered interleaving, and a drop-queue
would SILENTLY LOSE stage events (worse UX) to decouple from backpressure FastAPI handles correctly.
Revisit only if a stage is ever produced off the generator's own coroutine.
"""

import time

from app.rag.events import SSEEvent, stage_event

# The full pipeline stage order (design §3.6 / CLAUDE.md pipeline order). F17 adds only the leading
# `summarizing_memory`; the rest are emitted by baseline/F5–F9 unchanged.
STAGE_SEQUENCE = (
    "summarizing_memory", "rewriting", "cache_lookup", "searching",
    "reranking", "compressing", "generating", "citing",
)

MEMORY_STAGE = "summarizing_memory"


def emit(stage: str, status: str, ms: int | None = None) -> SSEEvent:
    """Build one `stage` SSE event. `status` ∈ {started, done, skipped}; `ms` populated on done."""
    return stage_event(stage, status, ms=ms)


class Timer:
    """Elapsed-ms helper for a `started`→`done` stage span (monotonic, not wall clock)."""

    def __init__(self) -> None:
        self._t0 = time.monotonic()

    def ms(self) -> int:
        return int((time.monotonic() - self._t0) * 1000)
