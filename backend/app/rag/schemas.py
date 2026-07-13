"""F3-local re-exports of the shared contracts (design.md §1).

`PipelineFlags` itself lives in `app.core.contracts` (not here) so `AnswerResponse` can reference
it without a circular import between that module and this one — this module re-exports it so
`app/rag/*` code can `from app.rag.schemas import PipelineFlags, RetrievedChunk, ...` without
reaching into `app.core` directly.
"""

from app.core.contracts import (
    AnswerResponse,
    ChatMessage,
    Chunk,
    Citation,
    MemoryContext,
    PipelineFlags,
    RetrievedChunk,
    StageEvent,
)

__all__ = [
    "AnswerResponse",
    "ChatMessage",
    "Chunk",
    "Citation",
    "MemoryContext",
    "PipelineFlags",
    "RetrievedChunk",
    "StageEvent",
]
