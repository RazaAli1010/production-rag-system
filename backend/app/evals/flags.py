"""`--flags` parsing (T3, AC-20/27).

`parse_flags("hybrid=on,rerank=off")` -> `PipelineFlags`. Two hard rules:
- an unknown flag key aborts with `ValueError` rather than being silently ignored (AC-20);
- `cache` is ALWAYS forced `False` regardless of the input string, because the F4 harness must run
  cache-bypassed so retrieval/generation metrics stay comparable across labels (AC-27, the CLAUDE.md
  "harness always runs skip_cache=true" rule). `session_id=None` is enforced separately at each
  suite's `answer()`/`astream()` call site.
"""

from app.core.contracts import PipelineFlags

_TRUE = {"on", "true", "1", "yes"}
_FALSE = {"off", "false", "0", "no"}
_VALID_KEYS = set(PipelineFlags.model_fields)


def parse_flags(spec: str | None) -> PipelineFlags:
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

    values["cache"] = False  # AC-27: never let the harness measure a cache-hit path
    return PipelineFlags(**values)
