"""Markdown report writer (T10, AC-22).

Renders `docs/eval_results/{label}.md` — a header stamped with label / git SHA / index manifest /
flags, then one table per suite (metric × slice). Written via `aiofiles` (async file I/O).
"""

import json

import aiofiles

from app.evals.schemas import EvalRunReport


def _render(report: EvalRunReport) -> str:
    lines: list[str] = [
        f"# Eval report — `{report.label}`",
        "",
        f"- **git SHA:** `{report.git_sha}`",
        f"- **pipeline flags:** `{json.dumps(report.pipeline_flags)}`",
        f"- **index manifest:** `{json.dumps(report.index_manifest)}`",
        "",
    ]
    for suite in report.suites:
        lines.append(f"## {suite.suite}")
        lines.append("")
        if not suite.metrics:
            lines.append("_(no metrics — suite skipped or produced no records)_")
            lines.append("")
            continue
        lines.append("| metric | slice | value |")
        lines.append("|---|---|---|")
        for m in suite.metrics:
            lines.append(f"| {m.metric} | {m.slice_tag or 'overall'} | {m.value:.4f} |")
        lines.append("")
    return "\n".join(lines)


async def write_report(report: EvalRunReport, settings) -> str:
    out_dir = settings.EVAL_RESULTS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{report.label}.md"
    async with aiofiles.open(path, "w", encoding="utf-8") as f:
        await f.write(_render(report))
    report.report_path = str(path)
    return str(path)
