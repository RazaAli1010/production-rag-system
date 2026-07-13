"""`eval_runs`, `eval_results` — F4 state (design.md §3.6)."""

import uuid

from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import CreatedAt, JSONBDict, UUIDpk


class EvalRun(Base):
    __tablename__ = "eval_runs"

    id: Mapped[UUIDpk]
    label: Mapped[str]
    git_sha: Mapped[str]
    index_manifest: Mapped[JSONBDict]
    pipeline_flags: Mapped[JSONBDict]
    started_at: Mapped[CreatedAt]


class EvalResult(Base):
    __tablename__ = "eval_results"

    id: Mapped[UUIDpk]
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("eval_runs.id", ondelete="CASCADE"))
    metric: Mapped[str]  # hit@5, mrr, faithfulness, answer_relevancy, latency, cost
    value: Mapped[float]
    slice_tag: Mapped[str | None]  # e.g. code_switched
