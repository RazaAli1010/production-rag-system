"""`--flags` parsing (T3, AC-20/27).

`parse_flags("hybrid=on,rerank=off")` -> `PipelineFlags`. Two hard rules:
- an unknown flag key aborts with `ValueError` rather than being silently ignored (AC-20);
- `cache` is forced `False` unless the caller explicitly opts in via `allow_cache` (AC-27, the
  CLAUDE.md "harness always runs skip_cache=true" rule). `session_id=None` is enforced separately at
  each suite's `answer()`/`astream()` call site.

**Why `allow_cache` exists (F9).** The cache-off rule was unconditional, and its stated rationale is
"so retrieval metrics stay comparable across labels" — which is exactly right for the retrieval,
RAGAS and refusal suites: a cached answer makes hit@k meaningless. But taken literally it also made
F9's own eval gate impossible, because that gate IS latency/cost on a repeat-heavy workload, and
CLAUDE.md itself scopes F9's gate to "latency/cost suites only". So the rule is narrowed to exactly
its rationale rather than dropped: `run.py` passes `allow_cache=True` only when the run is
`--suite latency` alone. `--suite all` still forces cache off, so the default and the safe path are
unchanged and no retrieval number can ever be measured through a cache hit.
"""

from app.core.contracts import PipelineFlags

_TRUE = {"on", "true", "1", "yes"}
_FALSE = {"off", "false", "0", "no"}
_VALID_KEYS = set(PipelineFlags.model_fields)


def parse_flags(spec: str | None, *, allow_cache: bool = False) -> PipelineFlags:
    values: dict[str, bool] = {}
    if spec:
        for pair in spec.split(","):
            pair = pair.strip()
            if not pair:
                continue
            if "=" not in pair:
                raise ValueError(f"malformed flag '{pair}' (expected key=on/off)")
            key, _, raw = pair.partition("=")
            key, raw = key.strip(), raw.strip().lower()
            if key not in _VALID_KEYS:
                raise ValueError(
                    f"unknown flag '{key}'; valid flags: {sorted(_VALID_KEYS)}"
                )
            if raw in _TRUE:
                values[key] = True
            elif raw in _FALSE:
                values[key] = False
            else:
                raise ValueError(f"flag '{key}' has non-boolean value '{raw}'")

    if not allow_cache:
        # AC-27/AC-32: never let retrieval/RAGAS/refusal measure a cache-hit path. Default-deny —
        # opting in is the caller's explicit act, not something a --flags string can do.
        values["cache"] = False
    return PipelineFlags(**values)
