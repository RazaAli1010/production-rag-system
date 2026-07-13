import pytest

from app.core.settings import Settings
from app.rag import errors


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


class _Counter:
    def __init__(self, fail_times, status_code=429):
        self.calls = 0
        self.fail_times = fail_times
        self.status_code = status_code

    async def __call__(self):
        self.calls += 1
        if self.calls <= self.fail_times:
            err = Exception("boom")
            err.status_code = self.status_code
            raise err
        return "ok"


async def test_retries_twice_then_raises_provider_error_on_exhaustion():
    settings = _settings(LLM_MAX_RETRIES=2)
    fn = _Counter(fail_times=99, status_code=429)  # always fails
    with pytest.raises(errors.ProviderError):
        await errors.call_with_retry(fn, settings=settings)
    assert fn.calls == 3  # initial attempt + 2 retries


async def test_succeeds_after_retrying_within_budget():
    settings = _settings(LLM_MAX_RETRIES=2)
    fn = _Counter(fail_times=1, status_code=429)
    result = await errors.call_with_retry(fn, settings=settings)
    assert result == "ok"
    assert fn.calls == 2


async def test_non_retryable_error_raises_immediately():
    settings = _settings(LLM_MAX_RETRIES=2)
    fn = _Counter(fail_times=99, status_code=400)
    with pytest.raises(Exception, match="boom"):
        await errors.call_with_retry(fn, settings=settings)
    assert fn.calls == 1  # no retry attempted
