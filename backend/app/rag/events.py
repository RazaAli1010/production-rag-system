"""SSE event shaping — the exact ordered contract producers/consumers agree on (design.md §4,
CLAUDE.md "SSE contract"): `stage*` -> `token*` -> `citations` -> `meta` -> `done`|`error`.
"""

from typing import Literal

from pydantic import BaseModel

from app.core.contracts import StageEvent


class SSEEvent(BaseModel):
    event: Literal["stage", "token", "citations", "meta", "done", "error"]
    data: dict


def stage_event(stage: str, status: str, ms: int | None = None,
                detail: dict | None = None) -> SSEEvent:
    data = StageEvent(stage=stage, status=status, ms=ms, detail=detail).model_dump()
    if detail is None:
        # Omitted rather than sent as null, so a frame with nothing to show stays byte-identical to
        # the pre-trace contract — every existing consumer and frame assertion is untouched, and
        # `ENABLE_TRACE=false` adds not one byte to the stream.
        data.pop("detail")
    return SSEEvent(event="stage", data=data)
