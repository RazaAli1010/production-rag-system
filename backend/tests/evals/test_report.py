"""T10 — markdown report writer."""

from app.evals.report import _render, write_report
from app.evals.schemas import EvalRunReport, MetricValue, SuiteResult
from tests.evals.conftest import make_settings


def _report():
    return EvalRunReport(
        label="baseline", git_sha="abc123", index_manifest={"strategy": "fixed"},
        pipeline_flags={"hybrid": False},
        suites=[
            SuiteResult(suite="retrieval", metrics=[
                MetricValue(metric="hit@5", value=0.8),
                MetricValue(metric="hit@5", value=0.6, slice_tag="code_switched"),
            ]),
            SuiteResult(suite="ragas", metrics=[]),
        ],
    )


def test_render_contains_header_and_rows():
    md = _render(_report())
    assert "# Eval report — `baseline`" in md
    assert "abc123" in md
    assert "| hit@5 | overall | 0.8000 |" in md
    assert "| hit@5 | code_switched | 0.6000 |" in md
    assert "_(no metrics" in md  # empty ragas suite


async def test_write_report_creates_file(tmp_path):
    s = make_settings(EVAL_RESULTS_DIR=tmp_path / "eval_results")
    report = _report()
    path = await write_report(report, s)
    assert path.endswith("baseline.md")
    assert (tmp_path / "eval_results" / "baseline.md").read_text(encoding="utf-8").startswith(
        "# Eval report"
    )
    assert report.report_path == path
