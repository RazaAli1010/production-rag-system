"""Generation suite — RAGAS 4 metrics (T5-6, AC-9/10/11/12).

Measures through the F3 seams: each answerable record's answer comes from `baseline.answer` and its
contexts from `retriever.retrieve` (design.md §2.1 — a standalone deterministic retrieve() rather
than an F3 signature change). Refusal probes are excluded. A judge-cost preview + confirm gate runs
*before* any generation so declining spends nothing (AC-11). RAGAS's synchronous `evaluate()` is
offloaded via `anyio.to_thread.run_sync` so the blocking judge sweep never runs on the loop (AC-12).
"""

import asyncio
import functools

import anyio
import structlog
import tiktoken

from app.evals import _ragas_compat  # noqa: F401  # installs the sunset-Vertex sys.modules shim
from app.evals.schemas import EvalRecord, MetricValue, SuiteResult
from app.indexing.cost import estimate_cost
from app.rag import baseline
from app.rag import retriever as retriever_mod

logger = structlog.get_logger(__name__)
_ENC = tiktoken.get_encoding("cl100k_base")

# canonical eval-gate metric name -> the ragas metric column name (`metric.name`).
_METRIC_COLUMNS = {
    "faithfulness": "faithfulness",
    "answer_relevancy": "answer_relevancy",
    "context_precision": "llm_context_precision_with_reference",
    "context_recall": "context_recall",
}


def preview_judge_cost(records: list[EvalRecord], settings) -> tuple[int, float]:
    """Estimate judge tokens + USD from the pre-generation inputs (question + ground_truth) scaled
    by `EVAL_RAGAS_JUDGE_MULTIPLIER` (AC-11). An intentional pre-flight approximation: it runs
    *before* answers/contexts are generated (so declining truly spends nothing), and the multiplier
    absorbs both RAGAS's several judge prompts per record and the not-yet-known answer/context
    expansion. Uses the central `estimate_cost` so preview and actuals share one cost model."""
    answerable = [r for r in records if not r.is_out_of_corpus]
    base_in = sum(len(_ENC.encode(r.question + " " + r.ground_truth_answer)) for r in answerable)
    tokens_in = int(base_in * settings.EVAL_RAGAS_JUDGE_MULTIPLIER)
    tokens_out = int(tokens_in * 0.25)  # judge reasoning/verdicts are short relative to the prompt
    usd = estimate_cost(settings.EVAL_JUDGE_MODEL, tokens_in, tokens_out)
    return tokens_in + tokens_out, usd


async def _sample_inputs(rec, flags, settings, sessionmaker, answer, retrieve, sem):
    async with sem:
        contexts_task = retrieve(rec.question, settings.EVAL_RETRIEVAL_K, None, settings)
        async with sessionmaker() as session:
            resp = await answer(
                rec.question, settings.EVAL_RETRIEVAL_K, None, flags,
                session=session, settings=settings,
            )
        chunks = await contexts_task
    return {
        "user_input": rec.question,
        "response": resp.answer,
        "retrieved_contexts": [c.text for c in chunks] or [""],
        "reference": rec.ground_truth_answer,
    }


def _build_judge(settings):
    from langchain_openai import ChatOpenAI, OpenAIEmbeddings
    from ragas.embeddings import LangchainEmbeddingsWrapper
    from ragas.llms import LangchainLLMWrapper

    key = settings.OPENAI_API_KEY.get_secret_value()
    llm = LangchainLLMWrapper(
        ChatOpenAI(model=settings.EVAL_JUDGE_MODEL, temperature=0, api_key=key)
    )
    emb = LangchainEmbeddingsWrapper(OpenAIEmbeddings(model=settings.EMBED_MODEL, api_key=key))
    return llm, emb


def _build_metrics():
    from ragas.metrics import (
        Faithfulness,
        LLMContextPrecisionWithReference,
        LLMContextRecall,
        ResponseRelevancy,
    )

    # canonical name -> ragas metric instance (order defines the emitted metric rows).
    return {
        "faithfulness": Faithfulness(),
        "answer_relevancy": ResponseRelevancy(),
        "context_precision": LLMContextPrecisionWithReference(),
        "context_recall": LLMContextRecall(),
    }


def _evaluate_sync(samples, llm, emb):
    """Blocking RAGAS call — only ever invoked inside `anyio.to_thread.run_sync` (AC-12)."""
    from ragas import EvaluationDataset, SingleTurnSample, evaluate

    metrics = _build_metrics()
    dataset = EvaluationDataset(samples=[SingleTurnSample(**s) for s in samples])
    result = evaluate(dataset=dataset, metrics=list(metrics.values()), llm=llm, embeddings=emb)
    df = result.to_pandas()
    scores = {name: float(df[col].mean()) for name, col in _METRIC_COLUMNS.items() if col in df}
    try:
        tokens = int(result.total_tokens())
    except Exception:
        tokens = None
    return scores, tokens


async def run_ragas(
    records: list[EvalRecord],
    flags,
    settings,
    *,
    confirm: bool,
    sessionmaker,
    answer=None,
    retrieve=None,
) -> SuiteResult:
    answer = answer or baseline.answer
    retrieve = retrieve or retriever_mod.retrieve
    tokens_est, usd_est = preview_judge_cost(records, settings)
    print(
        f"[ragas] judge-cost preview: ~{tokens_est} tokens, ~${usd_est:.4f} "
        f"(model={settings.EVAL_JUDGE_MODEL})"
    )
    if not confirm:
        logger.warning("evals.ragas.aborted_no_confirm", est_tokens=tokens_est, est_usd=usd_est)
        print("[ragas] skipped (no --yes / confirm) — no answers generated, no judge calls made.")
        return SuiteResult(suite="ragas", metrics=[])

    answerable = [r for r in records if not r.is_out_of_corpus]
    sem = asyncio.Semaphore(settings.EVAL_CONCURRENCY)
    samples = await asyncio.gather(
        *(_sample_inputs(r, flags, settings, sessionmaker, answer, retrieve, sem)
          for r in answerable)
    )

    llm, emb = _build_judge(settings)
    scores, tokens = await anyio.to_thread.run_sync(
        functools.partial(_evaluate_sync, list(samples), llm, emb)
    )

    if tokens is not None:
        logger.info("evals.ragas.judge_cost", tokens=tokens,
                    est_usd=estimate_cost(settings.EVAL_JUDGE_MODEL, tokens, 0))
    else:
        logger.info("evals.ragas.judge_cost", est_tokens=tokens_est, est_usd=usd_est)

    metrics = [MetricValue(metric=name, value=score) for name, score in scores.items()]
    return SuiteResult(suite="ragas", metrics=metrics)
