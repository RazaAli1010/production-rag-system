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


class FakeCallbackHandler:
    """Stands in for langfuse>=4's `langfuse.langchain.CallbackHandler`, which takes no
    credentials — it resolves the process-wide client `init_langfuse` builds."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs


def _fake_langfuse(monkeypatch):
    fake_pkg = types.ModuleType("langfuse")
    fake_langchain_mod = types.ModuleType("langfuse.langchain")
    fake_langchain_mod.CallbackHandler = FakeCallbackHandler
    monkeypatch.setitem(sys.modules, "langfuse", fake_pkg)
    monkeypatch.setitem(sys.modules, "langfuse.langchain", fake_langchain_mod)


def test_langfuse_config_empty_when_keys_absent():
    settings = _settings()  # LANGFUSE_PUBLIC_KEY/SECRET_KEY default to None
    assert observability.langfuse_config(None, settings) == {}


def test_langfuse_config_empty_when_only_one_key_present():
    settings = _settings(LANGFUSE_PUBLIC_KEY="pub")
    assert observability.langfuse_config(None, settings) == {}


def test_langfuse_config_carries_callback_and_trace_metadata(monkeypatch):
    _fake_langfuse(monkeypatch)

    settings = _settings(LANGFUSE_PUBLIC_KEY="pub", LANGFUSE_SECRET_KEY="sec")
    config = observability.langfuse_config("sess-1", settings)

    assert len(config["callbacks"]) == 1
    assert isinstance(config["callbacks"][0], FakeCallbackHandler)
    # v4 carries trace attributes as `langfuse_*` metadata keys, not constructor kwargs.
    assert config["metadata"]["langfuse_session_id"] == "sess-1"
    assert config["metadata"]["langfuse_trace_name"] == "ask"
    # The runbook's `metadata.request_id` filter depends on this staying a plain key.
    assert "request_id" in config["metadata"]


def test_langfuse_config_omits_session_key_when_anonymous(monkeypatch):
    # A null session_id must not be sent as `langfuse_session_id: None` — that would group every
    # anonymous ask into one bogus shared session in the UI.
    _fake_langfuse(monkeypatch)

    settings = _settings(LANGFUSE_PUBLIC_KEY="pub", LANGFUSE_SECRET_KEY="sec")
    config = observability.langfuse_config(None, settings)

    assert "langfuse_session_id" not in config["metadata"]


def test_langfuse_config_empty_when_package_not_installed(monkeypatch):
    # A `None` entry in sys.modules forces Python's import machinery to raise ImportError,
    # regardless of whether `langfuse` is actually installed in this environment.
    monkeypatch.setitem(sys.modules, "langfuse.langchain", None)
    settings = _settings(LANGFUSE_PUBLIC_KEY="pub", LANGFUSE_SECRET_KEY="sec")
    assert observability.langfuse_config(None, settings) == {}


def test_init_langfuse_builds_client_from_settings(monkeypatch):
    built = []

    fake_pkg = types.ModuleType("langfuse")
    fake_pkg.Langfuse = lambda **kw: built.append(kw)
    monkeypatch.setitem(sys.modules, "langfuse", fake_pkg)

    observability.init_langfuse(
        _settings(LANGFUSE_PUBLIC_KEY="pub", LANGFUSE_SECRET_KEY="sec",
                  LANGFUSE_BASE_URL="https://us.cloud.langfuse.com", APP_ENV="prod")
    )

    assert len(built) == 1
    # Credentials must be passed explicitly: pydantic-settings reads .env without exporting to
    # os.environ, so the SDK's own env auto-config would find nothing.
    assert built[0]["public_key"] == "pub"
    assert built[0]["secret_key"] == "sec"
    assert built[0]["base_url"] == "https://us.cloud.langfuse.com"
    assert built[0]["environment"] == "prod"


def test_init_langfuse_noop_without_keys(monkeypatch):
    fake_pkg = types.ModuleType("langfuse")

    def _boom(**kw):
        raise AssertionError("must not construct a client without keys")

    fake_pkg.Langfuse = _boom
    monkeypatch.setitem(sys.modules, "langfuse", fake_pkg)

    observability.init_langfuse(_settings())  # no keys → returns before importing/constructing


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
