"""Compat shim for importing RAGAS on the langchain 1.x stack (the "isolate" half of the
required-dep decision).

`ragas==0.4.3`'s `ragas.llms.base` unconditionally does
`from langchain_community.chat_models.vertexai import ChatVertexAI` at module-import time. The
sunset `langchain-community==0.4.2` this project pins no longer ships that path
(`chat_models.vertexai` was removed on the way to standalone integration packages). We never use
Vertex — the RAGAS judge is `ChatOpenAI` — so importing this module first registers a one-module
stub in `sys.modules` so the unconditional import resolves. `langchain_community.llms.VertexAI`
still exists in 0.4.2, so only the `chat_models.vertexai` submodule needs stubbing.

Import this module *before* `import ragas` anywhere.
"""

import sys
import types

_MODULE = "langchain_community.chat_models.vertexai"

if _MODULE not in sys.modules:
    try:  # pragma: no cover - trivial availability probe
        import langchain_community.chat_models.vertexai  # noqa: F401
    except ModuleNotFoundError:
        _stub = types.ModuleType(_MODULE)
        # Placeholder: referenced by ragas.llms.base's MULTIPLE_COMPLETION_SUPPORTED list but never
        # instantiated in an OpenAI-judge run.
        _stub.ChatVertexAI = type("ChatVertexAI", (), {})
        sys.modules[_MODULE] = _stub
