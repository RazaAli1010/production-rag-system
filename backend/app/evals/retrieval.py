"""Retrieval suite — hit@k + MRR (T4, AC-5/6/7/8).

Drives the F3->F5 seam `app.rag.retriever.retrieve` directly (injected as a default kwarg so tests
spy on it and F5/F6 re-measure with zero change). No LLM call — this is the cheap suite. Scores
overall (all answerable records) and per tag slice; `out_of_corpus` records carry no labeled source
and are excluded entirely (they belong to the refusal suite).
"""

import asyncio

from app.core.contracts import RetrievedChunk
from app.evals.schemas import EvalRecord, MetricValue, SuiteResult
from app.rag import retriever as retriever_mod

# Tags that name a measurable retrieval slice (AC-7). `out_of_corpus` is not here — it's excluded.
_SLICE_TAGS = ["en", "code_switched", "multi_doc", "table_lookup"]


def _is_hit(chunk: RetrievedChunk, rec: EvalRecord) -> bool:
    """A retrieved chunk hits when its doc_id matches a labeled source AND its page range or anchor
    overlaps the labeled pages/anchors (AC-6, design.md §5)."""
    if chunk.doc_id not in rec.source_doc_ids:
        return False
    labels = set(rec.source_pages_or_anchors)
    if not labels:
        # Doc-level label only (no page/anchor given): doc_id match is sufficient.
        return True
    if chunk.anchor and chunk.anchor in labels:
        return True
    if chunk.page_start is not None:
        end = chunk.page_end if chunk.page_end is not None else chunk.page_start
        return any(str(p) in labels for p in range(chunk.page_start, end + 1))
    return False


def _hit_at_k(ranked: list[RetrievedChunk], rec: EvalRecord, k: int) -> float:
    return 1.0 if any(_is_hit(c, rec) for c in ranked[:k]) else 0.0


def _reciprocal_rank(ranked: list[RetrievedChunk], rec: EvalRecord) -> float:
    for i, chunk in enumerate(ranked, start=1):
        if _is_hit(chunk, rec):
            return 1.0 / i
    return 0.0


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


async def run_retrieval(
    records: list[EvalRecord],
    flags,  # accepted for a uniform suite signature; retrieve() reads toggles from settings (F5+)
    settings,
    *,
    retrieve=None,
) -> SuiteResult:
    # Resolve the seam at call time (not a def-time default) so monkeypatching `retriever.retrieve`
    # in an end-to-end run.main test takes effect, while direct unit tests can still inject a spy.
    retrieve = retrieve or retriever_mod.retrieve
    answerable = [r for r in records if not r.is_out_of_corpus]
    k_max = settings.EVAL_RETRIEVAL_K
    sem = asyncio.Semaphore(settings.EVAL_CONCURRENCY)

    async def _rank(rec: EvalRecord) -> tuple[EvalRecord, list[RetrievedChunk]]:
        async with sem:
            ranked = await retrieve(rec.question, k_max, None, settings)
        return rec, ranked

    ranked_by_record = await asyncio.gather(*(_rank(r) for r in answerable))

    metrics: list[MetricValue] = []

    def _emit(slice_tag: str | None, pairs: list[tuple[EvalRecord, list[RetrievedChunk]]]) -> None:
        if not pairs:
            return
        for k in settings.EVAL_HIT_KS:
            metrics.append(MetricValue(
                metric=f"hit@{k}",
                value=_mean([_hit_at_k(ranked, rec, k) for rec, ranked in pairs]),
                slice_tag=slice_tag,
            ))
        metrics.append(MetricValue(
            metric="mrr",
            value=_mean([_reciprocal_rank(ranked, rec) for rec, ranked in pairs]),
            slice_tag=slice_tag,
        ))

    _emit(None, ranked_by_record)  # overall
    for tag in _SLICE_TAGS:
        _emit(tag, [(rec, ranked) for rec, ranked in ranked_by_record if tag in rec.tags])

    return SuiteResult(suite="retrieval", metrics=metrics)
