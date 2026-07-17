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
    (F6); `flags.query_rewrite` maps to `ENABLE_QUERY_REWRITE` (F7); `flags.compression` maps to
    `ENABLE_COMPRESSION` (F8); `flags.cache` maps to `ENABLE_CACHE` (F9). `RETRIEVAL_MODE` (the
    eval-only override) stays untouched so a `bm25_only` diagnostic run still wins over the boolean
    flag. Same two call sites F5 wired (`baseline._pipeline_events` +
    `evals.retrieval.run_retrieval`) — F6/F7/F8/F9 add no new seam.

    `flags.cache=False` IS F9's `skip_cache` bypass until F11 adds the HTTP request field (which
    will map `skip_cache=true` -> `flags.cache=False`), so there is deliberately no second bypass
    mechanism to keep in sync."""
    return settings.model_copy(
        update={
            "ENABLE_HYBRID": flags.hybrid,
            "ENABLE_RERANK": flags.rerank,
            "ENABLE_QUERY_REWRITE": flags.query_rewrite,
            "ENABLE_COMPRESSION": flags.compression,
            "ENABLE_CACHE": flags.cache,
        }
    )
