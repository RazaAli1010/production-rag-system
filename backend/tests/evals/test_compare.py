"""T12 — comparison / delta report (render + direction unit; DB-backed join)."""

from pathlib import Path

import pytest

from app.db.models.evals import EvalResult, EvalRun
from app.evals import compare as C
from tests.evals.conftest import make_settings


def test_direction_higher_is_better():
    assert C._direction("hit@5", 0.1) == "▲"
    assert C._direction("hit@5", -0.1) == "▼"
    assert C._direction("hit@5", 0.0) == "="


def test_direction_lower_is_better_flips():
    assert C._direction("latency_p95", -5.0) == "▲"   # faster = improved
    assert C._direction("latency_p95", 5.0) == "▼"
    assert C._direction("cost_mean", -0.01) == "▲"
    assert C._direction("false_refusal_rate", -0.1) == "▲"


def test_direction_neutral_metric():
    assert C._direction("refusals_no_grounded_claims", 3.0) == "="


def test_render_table_marks_deltas():
    cur = {("hit@5", None): 0.81, ("latency_p95", None): 900.0}
    prv = {("hit@5", None): 0.72, ("latency_p95", None): 1000.0}
    table = C._render_table("f5-hybrid-after", "baseline", cur, prv)
    assert "hit@5 | overall | 0.7200 | 0.8100 | +0.0900 | ▲" in table
    assert "latency_p95 | overall | 1000.0000 | 900.0000 | -100.0000 | ▲" in table


async def _seed_run(sessionmaker, label, metrics):
    async with sessionmaker() as s:
        run = EvalRun(label=label, git_sha="sha", index_manifest={}, pipeline_flags={})
        s.add(run)
        await s.flush()
        for metric, value, slice_tag in metrics:
            s.add(EvalResult(run_id=run.id, metric=metric, value=value, slice_tag=slice_tag))
        await s.commit()


async def test_compare_labels_writes_delta(db_sessionmaker, tmp_path):
    await _seed_run(db_sessionmaker, "baseline", [("hit@5", 0.70, None)])
    await _seed_run(db_sessionmaker, "f5-hybrid-after", [("hit@5", 0.80, None)])
    s = make_settings(EVAL_RESULTS_DIR=tmp_path / "eval_results")
    path = await C.compare_labels("f5-hybrid-after", "baseline",
                                  settings=s, sessionmaker=db_sessionmaker)
    assert path.endswith("f5-hybrid-after-vs-baseline.md")
    text = Path(path).read_text(encoding="utf-8")
    assert "+0.1000" in text and "▲" in text


async def test_compare_self_is_all_flat(db_sessionmaker, tmp_path):
    await _seed_run(db_sessionmaker, "baseline", [("hit@5", 0.70, None)])
    s = make_settings(EVAL_RESULTS_DIR=tmp_path / "eval_results")
    await C.compare_labels("baseline", "baseline", settings=s, sessionmaker=db_sessionmaker)
    text = (tmp_path / "eval_results" / "baseline-vs-baseline.md").read_text(encoding="utf-8")
    assert "+0.0000 | =" in text


async def test_compare_missing_label_exits(db_sessionmaker, tmp_path):
    await _seed_run(db_sessionmaker, "baseline", [("hit@5", 0.70, None)])
    s = make_settings(EVAL_RESULTS_DIR=tmp_path / "eval_results")
    with pytest.raises(SystemExit, match="nope"):
        await C.compare_labels("nope", "baseline", settings=s, sessionmaker=db_sessionmaker)
