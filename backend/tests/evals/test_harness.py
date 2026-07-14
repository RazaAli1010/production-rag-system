"""T9 — harness orchestration + persistence (DB)."""

from sqlalchemy import func, select

from app.core.contracts import PipelineFlags
from app.db.models.evals import EvalResult, EvalRun
from app.evals import harness
from app.evals.schemas import EvalConfig, MetricValue, SuiteResult
from tests.evals.conftest import FIXTURES_DIR, make_settings


def test_expand_suites():
    assert harness.expand_suites("all") == ["retrieval", "ragas", "refusal", "latency"]
    assert harness.expand_suites("retrieval") == ["retrieval"]


async def test_run_suites_persists_run_and_results(db_sessionmaker, monkeypatch):
    async def fake_retrieval(records, flags, settings):
        return SuiteResult(suite="retrieval", metrics=[
            MetricValue(metric="hit@5", value=0.8),
            MetricValue(metric="hit@5", value=0.6, slice_tag="code_switched"),
            MetricValue(metric="mrr", value=0.5),
        ])

    monkeypatch.setattr(harness, "run_retrieval", fake_retrieval)

    s = make_settings(EVAL_DATASET_PATH=FIXTURES_DIR / "run_dataset.jsonl")
    cfg = EvalConfig(label="baseline", flags=PipelineFlags(), suites=["retrieval"], confirm=False)
    report = await harness.run_suites(cfg, settings=s, sessionmaker=db_sessionmaker)

    assert report.label == "baseline"
    assert report.pipeline_flags["cache"] is False
    async with db_sessionmaker() as session:
        runs = await session.scalar(select(func.count()).select_from(EvalRun))
        results = await session.scalar(select(func.count()).select_from(EvalResult))
        run = await session.scalar(select(EvalRun).where(EvalRun.label == "baseline"))
    assert runs == 1
    assert results == 3
    assert run.git_sha  # captured (real sha or "unknown")
