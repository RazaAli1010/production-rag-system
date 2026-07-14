"""Refusal suite (T7, AC-13/14/15).

Drives `baseline.answer` over every record and reports:
- refusal_recall  = fraction of out_of_corpus probes correctly refused (higher is better);
- false_refusal_rate = fraction of answerable records wrongly refused (lower is better);
- a per-`refusal_reason` count so the F3/F6 threshold tuner sees whether the pre-LLM gate
  (`low_retrieval_confidence`) or the zero-citation guard (`no_grounded_claims`) fired.
"""

import asyncio
from collections import Counter

import structlog

from app.evals.schemas import EvalRecord, MetricValue, SuiteResult
from app.rag import baseline

logger = structlog.get_logger(__name__)


async def run_refusal(
    records: list[EvalRecord],
    flags,
    settings,
    *,
    sessionmaker,
    answer=None,
) -> SuiteResult:
    answer = answer or baseline.answer
    sem = asyncio.Semaphore(settings.EVAL_CONCURRENCY)

    async def _ask(rec: EvalRecord):
        async with sem:
            async with sessionmaker() as session:
                resp = await answer(
                    rec.question, settings.EVAL_RETRIEVAL_K, None, flags,
                    session=session, settings=settings,
                )
        return rec, resp

    results = await asyncio.gather(*(_ask(r) for r in records))

    ooc = [resp for rec, resp in results if rec.is_out_of_corpus]
    answerable = [resp for rec, resp in results if not rec.is_out_of_corpus]

    refusal_recall = (
        sum(1 for r in ooc if r.refused) / len(ooc) if ooc else 0.0
    )
    false_refusal_rate = (
        sum(1 for r in answerable if r.refused) / len(answerable) if answerable else 0.0
    )

    reason_counts = Counter(
        resp.refusal_reason for _, resp in results if resp.refused and resp.refusal_reason
    )

    metrics = [
        MetricValue(metric="refusal_recall", value=refusal_recall),
        MetricValue(metric="false_refusal_rate", value=false_refusal_rate),
    ]
    for reason, count in sorted(reason_counts.items()):
        metrics.append(MetricValue(metric=f"refusals_{reason}", value=float(count)))

    logger.info("evals.refusal.summary", refusal_recall=refusal_recall,
                false_refusal_rate=false_refusal_rate, reasons=dict(reason_counts))
    return SuiteResult(suite="refusal", metrics=metrics)
