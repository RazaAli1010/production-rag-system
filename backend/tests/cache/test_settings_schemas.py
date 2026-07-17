import pytest
from pydantic import ValidationError

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


def test_settings_loads_f9_defaults():
    s = _settings()
    assert s.ENABLE_CACHE is False  # AC-30: default-off, like every other enhancement flag
    assert s.REDIS_URL is None  # AC-4: Redis unconfigured => hot tier skipped, not a boot failure
    assert s.CACHE_REDIS_TTL_S == 86_400
    assert s.CACHE_REDIS_TIMEOUT_S == 0.25
    assert s.CACHE_KEY_PREFIX == "campusrag:cache:"
    # 0.86, NOT the originally specced 0.95 — calibrated at T7 against real embeddings, where
    # nothing in the adversarial set reaches 0.95 (the tier would never fire). See
    # tests/cache/test_adversarial.py.
    assert s.CACHE_SIMILARITY_THRESHOLD == 0.86
    assert "bs" in s.CACHE_DISCRIMINATIVE_TERMS and "mphil" in s.CACHE_DISCRIMINATIVE_TERMS
    # pu/hec deliberately absent — see test_keys.test_issuing_bodies_are_deliberately_not_...
    assert "pu" not in s.CACHE_DISCRIMINATIVE_TERMS
    assert s.CACHE_MAX_ENTRIES == 10_000
    # The specced lexical Jaccard floor measured inert once the discriminative veto exists, and was
    # dropped rather than shipped as a knob that never fires.
    assert not hasattr(s, "CACHE_LEXICAL_JACCARD_MIN")


def test_f9_reuses_f2_embed_settings_rather_than_redefining():
    # AC-5/design §7: the cache embeds with the SAME model/dim the corpus was indexed with — a
    # separate CACHE_EMBED_MODEL would let the query vector and the cached vectors drift apart.
    s = _settings()
    assert s.EMBED_MODEL == "text-embedding-3-small"
    assert s.EMBED_DIM == 1536
    assert not hasattr(s, "CACHE_EMBED_MODEL")


def test_settings_env_overrides():
    s = _settings(ENABLE_CACHE=True, CACHE_SIMILARITY_THRESHOLD=0.9, CACHE_MAX_ENTRIES=5)
    assert s.ENABLE_CACHE is True
    assert s.CACHE_SIMILARITY_THRESHOLD == 0.9
    assert s.CACHE_MAX_ENTRIES == 5


def test_redis_url_parses_when_set():
    s = _settings(REDIS_URL="redis://localhost:6379/0")
    assert s.REDIS_URL is not None
    assert str(s.REDIS_URL).startswith("redis://localhost:6379")


def test_redis_url_rejects_malformed_value():
    with pytest.raises(ValidationError):
        _settings(REDIS_URL="not-a-redis-url")
