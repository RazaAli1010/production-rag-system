"""Comparison — the eval-gate artifact (T12, AC-24/25/26).

`compare_labels(current, prev)` joins each label's most recent run on `(metric, slice_tag)` and
emits a metric-aware delta table (for latency/cost/false-refusal metrics *lower is better*, so the
direction arrow flips). Prints to stdout AND writes `docs/eval_results/{current}-vs-{prev}.md` — the
file each Phase B feature commits as its gate artifact. A missing label exits non-zero (AC-25).
"""

import sys

import aiofiles
from sqlalchemy import select

from app.db.models.evals import EvalResult, EvalRun

_EPS = 1e-9
# Metrics where a smaller value is the improvement (arrow flipped vs the default higher-is-better).
_LOWER_IS_BETTER_PREFIXES = ("latency_", "cost_", "tokens_mean", "false_refusal_rate")
# Metrics with no inherent good direction — reported flat regardless of delta sign.
_NEUTRAL_PREFIXES = ("refusals_",)


def _lower_is_better(metric: str) -> bool:
    return metric.startswith(_LOWER_IS_BETTER_PREFIXES)


def _neutral(metric: str) -> bool:
    return metric.startswith(_NEUTRAL_PREFIXES)


def _direction(metric: str, delta: float) -> str:
    if _neutral(metric) or abs(delta) < _EPS:
        return "="
    improved = delta < 0 if _lower_is_better(metric) else delta > 0
    return "▲" if improved else "▼"


async def _latest_results(label: str, *, sessionmaker) -> dict[tuple[str, str | None], float]:
    async with sessionmaker() as session:
        run = await session.scalar(
            select(EvalRun).where(EvalRun.label == label)
            .order_by(EvalRun.started_at.desc()).limit(1)
        )
        if run is None:
            raise SystemExit(f"error: no eval_runs row for label '{label}'")
        rows = (await session.scalars(
            select(EvalResult).where(EvalResult.run_id == run.id)
        )).all()
    return {(r.metric, r.slice_tag): r.value for r in rows}


def _render_table(current: str, prev: str, cur: dict, prv: dict) -> str:
    keys = sorted(set(cur) | set(prv), key=lambda k: (k[0], k[1] or ""))
    lines = [
        f"# Eval delta — `{current}` vs `{prev}`",
        "",
        f"| metric | slice | {prev} | {current} | Δ | dir |",
        "|---|---|---|---|---|---|",
    ]
    for metric, slice_tag in keys:
        p = prv.get((metric, slice_tag))
        c = cur.get((metric, slice_tag))
        if p is None or c is None:
            delta_s, dir_s = "—", "·"
            p_s = f"{p:.4f}" if p is not None else "—"
            c_s = f"{c:.4f}" if c is not None else "—"
        else:
            delta = c - p
            delta_s = f"{delta:+.4f}"
            dir_s = _direction(metric, delta)
            p_s, c_s = f"{p:.4f}", f"{c:.4f}"
        slice_s = slice_tag or "overall"
        lines.append(f"| {metric} | {slice_s} | {p_s} | {c_s} | {delta_s} | {dir_s} |")
    lines.append("")
    return "\n".join(lines)


async def compare_labels(current: str, prev: str, *, settings, sessionmaker) -> str:
    cur = await _latest_results(current, sessionmaker=sessionmaker)
    prv = await _latest_results(prev, sessionmaker=sessionmaker)
    table = _render_table(current, prev, cur, prv)
    print(table, file=sys.stdout)

    out_dir = settings.EVAL_RESULTS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{current}-vs-{prev}.md"
    async with aiofiles.open(path, "w", encoding="utf-8") as f:
        await f.write(table)
    return str(path)
