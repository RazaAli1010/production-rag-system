"""F4 CLI entrypoint (T11, AC-19/20).

    python -m app.evals.run --suite all --flags hybrid=on,rerank=off --label f5-hybrid-after [--yes]
    python -m app.evals.run --label f5-hybrid-after --compare baseline
    python -m app.evals.run --lint

`asyncio.run` entrypoint. Three modes: `--lint` (dataset quota check), `--compare` (delta table
between two labels), and a normal run (score suites -> persist -> write report).
"""

import argparse
import asyncio
import sys

import structlog

from app.core.settings import settings as default_settings
from app.db.engine import get_sessionmaker
from app.evals.compare import compare_labels
from app.evals.dataset import lint_dataset, load_dataset
from app.evals.flags import parse_flags
from app.evals.harness import expand_suites, run_suites
from app.evals.report import write_report
from app.evals.schemas import EvalConfig

logger = structlog.get_logger(__name__)


def _parse_args(argv):
    p = argparse.ArgumentParser(prog="app.evals.run")
    p.add_argument("--suite", choices=["retrieval", "ragas", "refusal", "latency", "all"])
    p.add_argument("--flags", default=None,
                   help="e.g. hybrid=on,rerank=off (cache always forced off)")
    p.add_argument("--label", default=None)
    p.add_argument("--compare", default=None, metavar="PREV_LABEL")
    p.add_argument("--lint", action="store_true")
    p.add_argument("--yes", action="store_true", help="confirm RAGAS judge spend without prompting")
    return p.parse_args(argv)


async def _lint(settings) -> int:
    records = await load_dataset(settings)
    reasons = lint_dataset(records, settings)
    if reasons:
        print(f"[lint] FAIL ({len(records)} records):", file=sys.stderr)
        for r in reasons:
            print(f"  - {r}", file=sys.stderr)
        return 1
    print(f"[lint] OK — {len(records)} records satisfy all quotas.")
    return 0


async def main(argv=None, settings=None, sessionmaker=None) -> int:
    args = _parse_args(argv)
    settings = settings or default_settings
    sessionmaker = sessionmaker or get_sessionmaker()

    if args.lint:
        return await _lint(settings)

    if args.compare:
        if not args.label:
            print("error: --compare requires --label (the current label)", file=sys.stderr)
            return 2
        path = await compare_labels(args.label, args.compare,
                                    settings=settings, sessionmaker=sessionmaker)
        print(f"[compare] wrote {path}")
        return 0

    if not args.suite or not args.label:
        print("error: --suite and --label are required for a run", file=sys.stderr)
        return 2

    try:
        flags = parse_flags(args.flags)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    cfg = EvalConfig(
        label=args.label,
        flags=flags,
        suites=expand_suites(args.suite),
        confirm=args.yes,
    )
    report = await run_suites(cfg, settings=settings, sessionmaker=sessionmaker)
    path = await write_report(report, settings)
    print(f"[run] label={report.label} sha={report.git_sha[:12]} -> {path}")
    return 0


def _entrypoint():
    raise SystemExit(asyncio.run(main()))


if __name__ == "__main__":
    _entrypoint()
