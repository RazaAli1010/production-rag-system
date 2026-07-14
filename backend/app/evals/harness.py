"""Harness orchestration + persistence (T9, AC-19/21/27).

`run_suites` expands `--suite`, runs each requested suite through the F3 seams, and persists one
`eval_runs` row (git SHA + index manifest + flags) plus one `eval_results` row per `(metric,
slice_tag)` — the F12-owned tables, so **no migration**. Cache is already forced off in
`flags.parse_flags`; `session_id=None` is inherent (F3's `answer`/`astream` take no session_id and
default it to `None` on `AnswerResponse`), so retrieval/generation metrics stay comparable (AC-27).
"""

import structlog

from app.db.models.evals import EvalResult, EvalRun
from app.evals import manifest as manifest_mod
from app.evals.dataset import load_dataset
from app.evals.latency import run_latency
from app.evals.ragas_suite import run_ragas
from app.evals.refusal import run_refusal
from app.evals.retrieval import run_retrieval
from app.evals.schemas import EvalConfig, EvalRunReport, SuiteResult

logger = structlog.get_logger(__name__)

ALL_SUITES = ["retrieval", "ragas", "refusal", "latency"]


def expand_suites(suite: str) -> list[str]:
    return list(ALL_SUITES) if suite == "all" else [suite]


async def run_suites(cfg: EvalConfig, *, settings, sessionmaker) -> EvalRunReport:
    records = await load_dataset(settings)

    suites: list[SuiteResult] = []
    for name in cfg.suites:
        if name == "retrieval":
            suites.append(await run_retrieval(records, cfg.flags, settings))
        elif name == "ragas":
            suites.append(await run_ragas(records, cfg.flags, settings,
                                          confirm=cfg.confirm, sessionmaker=sessionmaker))
        elif name == "refusal":
            suites.append(await run_refusal(records, cfg.flags, settings,
                                            sessionmaker=sessionmaker))
        elif name == "latency":
            suites.append(await run_latency(records, cfg.flags, settings,
                                            sessionmaker=sessionmaker))
        else:  # pragma: no cover - guarded by the CLI's --suite choices
            raise ValueError(f"unknown suite: {name}")

    report = EvalRunReport(
        label=cfg.label,
        git_sha=await manifest_mod.git_sha(),
        index_manifest=await manifest_mod.index_manifest_snapshot(settings),
        pipeline_flags=cfg.flags.model_dump(),
        suites=suites,
    )
    await _persist(report, sessionmaker=sessionmaker)
    return report


async def _persist(report: EvalRunReport, *, sessionmaker) -> None:
    async with sessionmaker() as session:
        run = EvalRun(
            label=report.label,
            git_sha=report.git_sha,
            index_manifest=report.index_manifest,
            pipeline_flags=report.pipeline_flags,
        )
        session.add(run)
        await session.flush()  # populate run.id for the FK
        for suite in report.suites:
            for m in suite.metrics:
                session.add(EvalResult(
                    run_id=run.id, metric=m.metric, value=m.value, slice_tag=m.slice_tag,
                ))
        await session.commit()
    logger.info("evals.persisted", label=report.label,
                metrics=sum(len(s.metrics) for s in report.suites))
