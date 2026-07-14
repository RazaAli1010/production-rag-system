"""Fixture-integrity guard (repo-friendly sizes + presence)."""

from pathlib import Path

from tests.evals.conftest import FIXTURES_DIR

SEED = Path("app/data/evals/qa_dataset.jsonl")


def test_fixture_files_present_and_small():
    for name in ["valid_dataset.jsonl", "dup_qid_dataset.jsonl", "run_dataset.jsonl"]:
        p = FIXTURES_DIR / name
        assert p.exists(), f"missing fixture {name}"
    total = sum(p.stat().st_size for p in FIXTURES_DIR.glob("*.jsonl"))
    assert total < 256 * 1024  # keep the repo lean


def test_seed_dataset_committed():
    assert SEED.exists(), "committed seed qa_dataset.jsonl must exist"
    assert SEED.stat().st_size < 64 * 1024
