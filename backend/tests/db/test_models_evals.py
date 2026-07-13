"""T-9: EvalRun/EvalResult — distinct metric/slice_tag rows + cascade delete."""

import pytest

from app.db.models import EvalResult, EvalRun


@pytest.mark.asyncio
async def test_eval_run_results_and_cascade(session):
    run = EvalRun(
        label="baseline",
        git_sha="deadbeef",
        index_manifest={"namespace": "pu", "chunks": 100},
        pipeline_flags={"hybrid": False},
    )
    session.add(run)
    await session.flush()

    results = [
        EvalResult(run_id=run.id, metric="hit@5", value=0.82, slice_tag=None),
        EvalResult(run_id=run.id, metric="hit@5", value=0.71, slice_tag="code_switched"),
        EvalResult(run_id=run.id, metric="mrr", value=0.65, slice_tag=None),
    ]
    session.add_all(results)
    await session.flush()

    result_ids = [r.id for r in results]

    await session.delete(run)
    await session.flush()

    # populate_existing bypasses the identity map so the assert reflects the DB-level cascade.
    for rid in result_ids:
        assert await session.get(EvalResult, rid, populate_existing=True) is None
