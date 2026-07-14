"""T1 — F4 Settings keys + evals schemas."""

from pathlib import Path

import pytest

from app.core.contracts import PipelineFlags
from app.evals.schemas import EvalConfig, EvalRecord, MetricValue
from tests.evals.conftest import make_settings


def test_settings_load_f4_keys():
    s = make_settings()
    assert s.EVAL_DATASET_PATH == Path("app/data/evals/qa_dataset.jsonl")
    assert s.EVAL_HIT_KS == [1, 3, 5]
    assert s.EVAL_JUDGE_MODEL == "gpt-4o-mini"
    assert s.EVAL_DATASET_MIN == 60 and s.EVAL_DATASET_MAX == 80
    assert s.EVAL_QUOTA_CODE_SWITCHED == 15 and s.EVAL_QUOTA_OUT_OF_CORPUS == 10


def test_settings_env_override():
    s = make_settings(EVAL_LATENCY_REQUESTS=25, EVAL_JUDGE_MODEL="gpt-4o")
    assert s.EVAL_LATENCY_REQUESTS == 25
    assert s.EVAL_JUDGE_MODEL == "gpt-4o"


def test_eval_record_coerces_pages_to_str_and_flags_ooc():
    r = EvalRecord(qid="q", question="x", ground_truth_answer="y",
                   source_doc_ids=["d"], source_pages_or_anchors=[12, "clause-1"], tags=["en"])
    assert r.source_pages_or_anchors == ["12", "clause-1"]
    assert r.is_out_of_corpus is False
    assert EvalRecord(qid="o", question="x", ground_truth_answer="y",
                      tags=["out_of_corpus"]).is_out_of_corpus is True


def test_eval_record_rejects_empty_tags():
    with pytest.raises(ValueError):
        EvalRecord(qid="q", question="x", ground_truth_answer="y", tags=[])


def test_eval_config_roundtrip():
    cfg = EvalConfig(label="baseline", flags=PipelineFlags(), suites=["retrieval"], confirm=True)
    assert cfg.model_dump()["flags"]["cache"] is False
    assert MetricValue(metric="hit@5", value=0.8).slice_tag is None
