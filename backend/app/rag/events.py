"""SSE event shaping — the exact ordered contract producers/consumers agree on (design.md §4,
CLAUDE.md "SSE contract"): `stage*` -> `token*` -> `citations` -> `meta` -> `done`|`error`.
"""

from typing import Literal

from pydantic import BaseModel

from app.core.contracts import StageEvent


class SSEEvent(BaseModel):
    event: Literal["stage", "token", "citations", "meta", "done", "error"]
    data: dict


def stage_event(stage: str, status: str, ms: int | None = None) -> SSEEvent:
    return SSEEvent(event="stage", data=StageEvent(stage=stage, status=status, ms=ms).model_dump())
