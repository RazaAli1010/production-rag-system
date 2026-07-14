"""F4 schemas (design.md §4).

`EvalRecord` is the on-disk QA row; `MetricValue`/`SuiteResult`/`EvalRunReport` are the transient
in-memory results a run produces before they are flattened into `eval_results` rows (one row per
`MetricValue`) and rendered to `docs/eval_results/{label}.md`. `EvalConfig` is the parsed CLI
intent.

Canonical pipeline models (`PipelineFlags`, etc.) live in `app.core.contracts` and are imported,
never redefined.
"""

from pydantic import BaseModel, field_validator

from app.core.contracts import PipelineFlags

OUT_OF_CORPUS_TAG = "out_of_corpus"


class EvalRecord(BaseModel):
    """One QA row from `qa_dataset.jsonl` (AC-1).

    `source_pages_or_anchors` holds page numbers (as strings) and/or HTML anchors interchangeably —
    hit scoring (`retrieval._is_hit`) matches a retrieved chunk's page range OR anchor against this
    set, so both live in one field. Coerced to `str` on load so `12` and `"12"` compare equal.
    """

    qid: str
    question: str
    ground_truth_answer: str
    source_doc_ids: list[str] = []
    source_pages_or_anchors: list[str] = []
    tags: list[str]

    @field_validator("source_pages_or_anchors", mode="before")
    @classmethod
    def _coerce_to_str(cls, v: object) -> object:
        if isinstance(v, list):
            return [str(x) for x in v]
        return v

    @field_validator("tags")
    @classmethod
    def _tags_non_empty(cls, v: list[str]) -> list[str]:
        # AC-4: every record maps to at least one measurable slice.
        if not v:
            raise ValueError("tags must be non-empty")
        return v

    @property
    def is_out_of_corpus(self) -> bool:
        return OUT_OF_CORPUS_TAG in self.tags


class MetricValue(BaseModel):
    """One scored metric, optionally sliced. `slice_tag=None` is the overall (all-records) value;
    a tag value (e.g. `"code_switched"`) is that slice. Flattens 1:1 to an `eval_results` row."""

    metric: str  # "hit@5", "mrr", "faithfulness", "latency_p95", "cost_mean", ...
    value: float
    slice_tag: str | None = None


class SuiteResult(BaseModel):
    suite: str  # retrieval | ragas | refusal | latency
    metrics: list[MetricValue] = []


class EvalRunReport(BaseModel):
    label: str
    git_sha: str
    index_manifest: dict = {}
    pipeline_flags: dict = {}
    suites: list[SuiteResult] = []
    report_path: str | None = None


class EvalConfig(BaseModel):
    """Parsed CLI intent for a single run (design.md §4)."""

    label: str
    flags: PipelineFlags  # cache always False — flags.parse_flags forces it (AC-27)
    suites: list[str]  # expanded from --suite ("all" -> the four)
    confirm: bool = False  # --yes: RAGAS cost-gate bypass (AC-11)
