"""Feature-level acceptance — requirements.md §4 (mocked seams, no live keys).

Proves F4 measures through the F3 seams: the retrieval suite calls `retriever.retrieve` and the
ragas/refusal suites call `baseline.answer` (spy assertions), so F5+ need no F4 change.
"""


from sqlalchemy import func, select

from app.core.contracts import AnswerResponse, RetrievedChunk
from app.db.models.evals import EvalResult, EvalRun
from app.evals import ragas_suite as RS
from app.evals import run as run_mod
from app.rag import baseline
from app.rag import retriever as retriever_mod
from app.rag.events import SSEEvent, stage_event
from tests.evals.conftest import FIXTURES_DIR, make_settings


def _install_fakes(monkeypatch, spy):
    async def fake_retrieve(q, k, ns, s):
        spy["retrieve"] += 1
        return [RetrievedChunk(chunk_id="pu-academic-probation-2023:0",
                               doc_id="pu-academic-probation-2023", title="t", text="ctx",
                               page_start=2, page_end=2, dense_score=0.9)]

    async def fake_answer(q, k, ns, flags, *, session, settings):
        spy["answer"] += 1
        return AnswerResponse(answer="Because [1].", pipeline_flags=flags)

    async def fake_astream(q, k, ns, flags, *, session, settings):
        spy["astream"] += 1
        yield stage_event("searching", "done", ms=5)
        yield SSEEvent(event="token", data={"token": "a"})
        yield stage_event("generating", "done", ms=20)
        yield SSEEvent(event="done", data={})

    monkeypatch.setattr(retriever_mod, "retrieve", fake_retrieve)
    monkeypatch.setattr(baseline, "answer", fake_answer)
    monkeypatch.setattr(baseline, "astream", fake_astream)
    # RAGAS: keep the judge offline
    monkeypatch.setattr(RS, "_build_judge", lambda s: (None, None))
    monkeypatch.setattr(RS, "_evaluate_sync", lambda samples, llm, emb: (
        {"faithfulness": 0.9, "answer_relevancy": 0.8,
         "context_precision": 0.7, "context_recall": 0.6}, 100))


def _settings(tmp_path):
    return make_settings(
        EVAL_DATASET_PATH=FIXTURES_DIR / "run_dataset.jsonl",
        EVAL_RESULTS_DIR=tmp_path / "eval_results",
        EVAL_LATENCY_REQUESTS=2,
    )


async def test_ac1_full_baseline_report(db_sessionmaker, tmp_path, monkeypatch):
    spy = {"retrieve": 0, "answer": 0, "astream": 0}
    _install_fakes(monkeypatch, spy)
    s = _settings(tmp_path)

    rc = await run_mod.main(["--suite", "all", "--label", "baseline", "--yes"],
                            settings=s, sessionmaker=db_sessionmaker)
    assert rc == 0
    assert (tmp_path / "eval_results" / "baseline.md").exists()
    # AC-5 seam proof: retrieval used retrieve; ragas/refusal used answer.
    assert spy["retrieve"] > 0 and spy["answer"] > 0 and spy["astream"] > 0

    async with db_sessionmaker() as session:
        run = await session.scalar(select(EvalRun).where(EvalRun.label == "baseline"))
        n_results = await session.scalar(select(func.count()).select_from(EvalResult))
    assert run is not None and run.git_sha
    assert n_results > 0


async def test_ac2_lint_pass_and_fail(tmp_path, db_sessionmaker):
    ok = make_settings(EVAL_DATASET_PATH=FIXTURES_DIR / "valid_dataset.jsonl")
    assert await run_mod.main(["--lint"], settings=ok, sessionmaker=db_sessionmaker) == 0

    bad = make_settings(EVAL_DATASET_PATH=FIXTURES_DIR / "dup_qid_dataset.jsonl")
    assert await run_mod.main(["--lint"], settings=bad, sessionmaker=db_sessionmaker) == 1


async def test_ac3_ragas_no_spend_without_yes(db_sessionmaker, tmp_path, monkeypatch, capsys):
    spy = {"retrieve": 0, "answer": 0, "astream": 0}
    _install_fakes(monkeypatch, spy)
    evaluated = {"n": 0}

    def counting_evaluate(samples, llm, emb):
        evaluated["n"] += 1
        return {}, 0

    monkeypatch.setattr(RS, "_evaluate_sync", counting_evaluate)
    s = _settings(tmp_path)

    rc = await run_mod.main(["--suite", "ragas", "--label", "baseline"],  # no --yes
                            settings=s, sessionmaker=db_sessionmaker)
    assert rc == 0
    assert evaluated["n"] == 0  # judge never ran
    assert "preview" in capsys.readouterr().out


async def test_ac4_compare_renders_delta(db_sessionmaker, tmp_path, monkeypatch):
    spy = {"retrieve": 0, "answer": 0, "astream": 0}
    _install_fakes(monkeypatch, spy)
    s = _settings(tmp_path)
    await run_mod.main(["--suite", "retrieval", "--label", "baseline"],
                       settings=s, sessionmaker=db_sessionmaker)
    await run_mod.main(["--suite", "retrieval", "--label", "f5-hybrid-after"],
                       settings=s, sessionmaker=db_sessionmaker)
    rc = await run_mod.main(["--label", "f5-hybrid-after", "--compare", "baseline"],
                            settings=s, sessionmaker=db_sessionmaker)
    assert rc == 0
    assert (tmp_path / "eval_results" / "f5-hybrid-after-vs-baseline.md").exists()


async def test_ac5_seam_usage_asserted(db_sessionmaker, tmp_path, monkeypatch):
    spy = {"retrieve": 0, "answer": 0, "astream": 0}
    _install_fakes(monkeypatch, spy)
    s = _settings(tmp_path)
    await run_mod.main(["--suite", "refusal", "--label", "baseline"],
                       settings=s, sessionmaker=db_sessionmaker)
    assert spy["answer"] > 0  # refusal suite drove baseline.answer
