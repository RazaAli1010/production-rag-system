from app.core.contracts import PipelineFlags
from app.core.settings import Settings


def _settings(**o):
    return Settings(
        _env_file=None,
        DATABASE_URL="postgresql+asyncpg://u:p@localhost:5432/db",
        ADMIN_EMAIL="a@b.c",
        ADMIN_PASSWORD="x",
        OPENAI_API_KEY="k",
        PINECONE_API_KEY="k",
        PINECONE_INDEX="i",
        **o,
    )


def test_settings_loads_f3_defaults():
    s = _settings()
    assert s.LLM_MODEL == "gpt-4o-mini"
    assert s.LLM_MAX_RETRIES == 2
    assert s.RETRIEVAL_K == 5
    assert s.RETRIEVAL_NAMESPACES == ["pu", "hec"]
    assert s.REFUSAL_DENSE_THRESHOLD == 0.25
    assert s.REFUSAL_SUGGESTION_COUNT == 3
    assert s.MAX_QUERY_TOKENS == 200
    assert s.CITATION_QUOTE_MAX_WORDS == 25
    assert "official" in s.DISCLAIMER_TEXT.lower()
    assert s.LANGFUSE_PUBLIC_KEY is None
    assert s.LANGFUSE_SECRET_KEY is None
    assert s.LANGFUSE_HOST == "https://cloud.langfuse.com"


def test_settings_env_overrides():
    s = _settings(LLM_MODEL="gpt-4o", RETRIEVAL_K=8, REFUSAL_DENSE_THRESHOLD=0.5)
    assert s.LLM_MODEL == "gpt-4o"
    assert s.RETRIEVAL_K == 8
    assert s.REFUSAL_DENSE_THRESHOLD == 0.5


def test_settings_langfuse_secrets_are_secretstr_when_set():
    s = _settings(LANGFUSE_PUBLIC_KEY="pub", LANGFUSE_SECRET_KEY="sec")
    assert s.LANGFUSE_PUBLIC_KEY.get_secret_value() == "pub"
    assert s.LANGFUSE_SECRET_KEY.get_secret_value() == "sec"


def test_pipeline_flags_round_trips():
    flags = PipelineFlags()
    assert flags.model_dump() == {
        "hybrid": False,
        "rerank": False,
        "cache": False,
        "memory": False,
    }
    restored = PipelineFlags.model_validate(flags.model_dump())
    assert restored == flags
