"""Dataset load + lint (T2, AC-1/2/3/4).

`load_dataset` reads the git-versioned JSONL via `aiofiles` (async file I/O per the CLAUDE.md
mandate) and validates each line into an `EvalRecord`. `lint_dataset` is a pure function returning
a list of human-readable failure reasons (empty list = pass) so the CLI can print every problem at
once rather than aborting on the first.
"""

import json

import aiofiles

from app.evals.schemas import OUT_OF_CORPUS_TAG, EvalRecord

CODE_SWITCHED_TAG = "code_switched"


async def load_dataset(settings) -> list[EvalRecord]:
    """Read + validate every line of `settings.EVAL_DATASET_PATH`.

    A malformed JSON line or a record failing `EvalRecord` validation aborts with the 1-indexed
    line number and (when available) the `qid`, so a broken dataset is diagnosable (design.md §11).
    """
    path = settings.EVAL_DATASET_PATH
    async with aiofiles.open(path, encoding="utf-8") as f:
        content = await f.read()

    records: list[EvalRecord] = []
    for lineno, line in enumerate(content.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{lineno}: malformed JSON: {exc}") from exc
        try:
            records.append(EvalRecord.model_validate(raw))
        except Exception as exc:
            qid = raw.get("qid", "?") if isinstance(raw, dict) else "?"
            raise ValueError(f"{path}:{lineno}: invalid record (qid={qid}): {exc}") from exc
    return records


def lint_dataset(records: list[EvalRecord], settings) -> list[str]:
    """Return a list of quota/range/integrity violations ([] = pass). Thresholds come from Settings
    so the spec's 60-80 / >=15 / >=10 gate is configurable, not hardcoded (AC-2/3/4)."""
    reasons: list[str] = []
    total = len(records)

    if total < settings.EVAL_DATASET_MIN or total > settings.EVAL_DATASET_MAX:
        reasons.append(
            f"record count {total} outside required range "
            f"[{settings.EVAL_DATASET_MIN}, {settings.EVAL_DATASET_MAX}]"
        )

    n_code_switched = sum(1 for r in records if CODE_SWITCHED_TAG in r.tags)
    if n_code_switched < settings.EVAL_QUOTA_CODE_SWITCHED:
        reasons.append(
            f"{CODE_SWITCHED_TAG} count {n_code_switched} below quota "
            f"{settings.EVAL_QUOTA_CODE_SWITCHED}"
        )

    n_out_of_corpus = sum(1 for r in records if OUT_OF_CORPUS_TAG in r.tags)
    if n_out_of_corpus < settings.EVAL_QUOTA_OUT_OF_CORPUS:
        reasons.append(
            f"{OUT_OF_CORPUS_TAG} count {n_out_of_corpus} below quota "
            f"{settings.EVAL_QUOTA_OUT_OF_CORPUS}"
        )

    seen: set[str] = set()
    dupes: set[str] = set()
    for r in records:
        if r.qid in seen:
            dupes.add(r.qid)
        seen.add(r.qid)
    if dupes:
        reasons.append(f"duplicate qids: {sorted(dupes)}")

    # Empty tags are rejected at EvalRecord validation, but a record loaded via another path could
    # still slip through — check defensively so lint is a complete integrity gate (AC-4).
    empty_tag_qids = [r.qid for r in records if not r.tags]
    if empty_tag_qids:
        reasons.append(f"records with empty tags: {empty_tag_qids}")

    return reasons
