import sys
import types

from app.core.settings import Settings
from app.rag import observability


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


def test_langfuse_handler_none_when_keys_absent():
    settings = _settings()  # LANGFUSE_PUBLIC_KEY/SECRET_KEY default to None
    assert observability.langfuse_handler(None, settings) is None


def test_langfuse_handler_none_when_only_one_key_present():
    settings = _settings(LANGFUSE_PUBLIC_KEY="pub")
    assert observability.langfuse_handler(None, settings) is None


def test_langfuse_handler_returns_handler_when_both_keys_present(monkeypatch):
    class FakeCallbackHandler:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    fake_pkg = types.ModuleType("langfuse")
    fake_callback_mod = types.ModuleType("langfuse.callback")
    fake_callback_mod.CallbackHandler = FakeCallbackHandler
    monkeypatch.setitem(sys.modules, "langfuse", fake_pkg)
    monkeypatch.setitem(sys.modules, "langfuse.callback", fake_callback_mod)

    settings = _settings(LANGFUSE_PUBLIC_KEY="pub", LANGFUSE_SECRET_KEY="sec")
    handler = observability.langfuse_handler("sess-1", settings)

    assert isinstance(handler, FakeCallbackHandler)
    assert handler.kwargs["public_key"] == "pub"
    assert handler.kwargs["secret_key"] == "sec"
    assert handler.kwargs["session_id"] == "sess-1"


def test_langfuse_handler_none_when_package_not_installed(monkeypatch):
    # A `None` entry in sys.modules forces Python's import machinery to raise ImportError,
    # regardless of whether `langfuse` is actually installed in this environment.
    monkeypatch.setitem(sys.modules, "langfuse.callback", None)
    settings = _settings(LANGFUSE_PUBLIC_KEY="pub", LANGFUSE_SECRET_KEY="sec")
    assert observability.langfuse_handler(None, settings) is None


async def test_log_llm_cost_logs_tokens_and_cost(monkeypatch):
    calls = []
    monkeypatch.setattr(observability.logger, "info", lambda *a, **kw: calls.append((a, kw)))

    await observability.log_llm_cost("gpt-4o-mini", 100, 50)

    assert len(calls) == 1
    _, kwargs = calls[0]
    assert kwargs["model"] == "gpt-4o-mini"
    assert kwargs["tokens_in"] == 100
    assert kwargs["tokens_out"] == 50
    assert kwargs["est_cost_usd"] > 0
