"""T2 — dataset loader + lint."""

from pathlib import Path

import pytest

from app.evals.dataset import lint_dataset, load_dataset
from tests.evals.conftest import FIXTURES_DIR, make_settings


async def test_load_valid_dataset():
    s = make_settings(EVAL_DATASET_PATH=FIXTURES_DIR / "valid_dataset.jsonl")
    records = await load_dataset(s)
    assert len(records) == 60
    assert all(r.tags for r in records)


async def test_lint_passes_on_quota_meeting_dataset():
    s = make_settings(EVAL_DATASET_PATH=FIXTURES_DIR / "valid_dataset.jsonl")
    records = await load_dataset(s)
    assert lint_dataset(records, s) == []


async def test_lint_flags_count_range():
    s = make_settings(EVAL_DATASET_PATH=FIXTURES_DIR / "valid_dataset.jsonl")
    records = await load_dataset(s)
    # tighten the range so the 60-record fixture is now "too many"
    s2 = make_settings(EVAL_DATASET_MIN=1, EVAL_DATASET_MAX=10)
    reasons = lint_dataset(records, s2)
    assert any("outside required range" in r for r in reasons)


async def test_lint_flags_code_switched_quota():
    s = make_settings(EVAL_DATASET_PATH=FIXTURES_DIR / "valid_dataset.jsonl",
                      EVAL_QUOTA_CODE_SWITCHED=999)
    records = await load_dataset(s)
    reasons = lint_dataset(records, s)
    assert any("code_switched" in r for r in reasons)


async def test_lint_flags_out_of_corpus_quota():
    s = make_settings(EVAL_DATASET_PATH=FIXTURES_DIR / "valid_dataset.jsonl",
                      EVAL_QUOTA_OUT_OF_CORPUS=999)
    records = await load_dataset(s)
    reasons = lint_dataset(records, s)
    assert any("out_of_corpus" in r for r in reasons)


async def test_lint_flags_duplicate_qid():
    s = make_settings(EVAL_DATASET_PATH=FIXTURES_DIR / "dup_qid_dataset.jsonl",
                      EVAL_DATASET_MIN=1, EVAL_DATASET_MAX=100,
                      EVAL_QUOTA_CODE_SWITCHED=0, EVAL_QUOTA_OUT_OF_CORPUS=0)
    records = await load_dataset(s)
    reasons = lint_dataset(records, s)
    assert any("duplicate qids" in r and "d1" in r for r in reasons)


async def test_load_aborts_on_malformed_json(tmp_path: Path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"qid": "ok", "question": "q", "ground_truth_answer": "a", "tags": ["en"]}\n'
                   '{not json}\n', encoding="utf-8")
    s = make_settings(EVAL_DATASET_PATH=bad)
    with pytest.raises(ValueError, match="malformed JSON"):
        await load_dataset(s)


async def test_seed_dataset_loads_even_if_under_quota():
    # The committed real seed must at least PARSE (lint under-quota is expected + documented).
    s = make_settings(EVAL_DATASET_PATH=Path("app/data/evals/qa_dataset.jsonl"))
    records = await load_dataset(s)
    assert len(records) >= 12
    tags = {t for r in records for t in r.tags}
    assert {"en", "code_switched", "out_of_corpus", "multi_doc", "table_lookup"} <= tags
