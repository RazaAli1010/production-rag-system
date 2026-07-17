"""T1 — F11 Settings defaults + AnswerResponse identity fields (AC-21)."""

from app.core.contracts import AnswerResponse, PipelineFlags
from tests.api.conftest import make_settings


def test_settings_defaults():
    s = make_settings()
    assert s.ENABLE_RATE_LIMIT is True
    assert s.REQUEST_TIMEOUT_S == 30.0
    assert s.HISTORY_PAGE_SIZE == 50
    assert s.GZIP_MIN_BYTES == 500
    assert s.RATE_LIMIT_WINDOW_S == 60
    assert s.CORS_ALLOW_ORIGINS == []
    assert s.LLM_DEEP_MODEL == "gpt-4o"


def test_answer_response_identity_fields_additive():
    r = AnswerResponse(answer="x", pipeline_flags=PipelineFlags())
    assert r.request_id is None
    assert r.latency_ms is None
