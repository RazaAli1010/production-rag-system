"""The single toggle overlay that maps request/eval `PipelineFlags` onto the effective retrieval
settings (design.md §9, AC-12).

Applied at exactly two seams — `baseline._pipeline_events` (request path + the F4 ragas/refusal/
latency suites, which drive retrieval through `answer()`) and `evals.retrieval.run_retrieval` (the
one suite that calls the `retrieve` seam directly) — so the CLAUDE.md "toggleable via a config/
request flag" rule holds and the F4 retrieval-suite comment's reserved "retrieve() reads toggles
from settings (F5+)" contract is fulfilled without changing how any suite *measures*.
"""

from app.core.contracts import PipelineFlags


def apply_flags(settings, flags: PipelineFlags):
    """Return a settings copy with the retrieval toggles reflected from `flags` (never mutates the
    input). `flags.hybrid` maps to `ENABLE_HYBRID` (F5); `flags.rerank` maps to `ENABLE_RERANK`
    (F6). `RETRIEVAL_MODE` (the eval-only override) stays untouched so a `bm25_only` diagnostic run
    still wins over the boolean flag. Same two call sites F5 wired
    (`baseline._pipeline_events` + `evals.retrieval.run_retrieval`) — F6 adds no new seam."""
    return settings.model_copy(
        update={"ENABLE_HYBRID": flags.hybrid, "ENABLE_RERANK": flags.rerank}
    )
