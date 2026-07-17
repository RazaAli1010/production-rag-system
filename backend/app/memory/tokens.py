"""tiktoken message counting (AC-13). Pure CPU, runs inline — no LLM call, no thread offload.

Same `cl100k_base` module-encoder pattern as `rag.baseline._ENC` and
`indexing.chunkers.base._ENC`; `cl100k_base` is exact for gpt-4o-mini / text-embedding-3-small,
which the "token counter drift vs actual" edge case asserts in tests.
"""

import tiktoken

_ENC = tiktoken.get_encoding("cl100k_base")


def count(text: str) -> int:
    return len(_ENC.encode(text))
