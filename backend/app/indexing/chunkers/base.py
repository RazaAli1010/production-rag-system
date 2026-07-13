from typing import Protocol

import structlog
import tiktoken

from app.core.contracts import Chunk

logger = structlog.get_logger(__name__)
_ENC = tiktoken.get_encoding("cl100k_base")


def count_tokens(text):
    return len(_ENC.encode(text))


def make_chunk_id(doc_id, seq):
    return f"{doc_id}:{seq}"


def truncate_to_limit(text, settings):
    encoded = _ENC.encode(text)
    if len(encoded) <= settings.EMBED_MAX_CHUNK_TOKENS:
        return text, len(encoded)
    kept = encoded[: settings.EMBED_MAX_CHUNK_TOKENS]
    logger.warning("indexing.chunk.truncated", limit=settings.EMBED_MAX_CHUNK_TOKENS,
                   original_tokens=len(encoded))
    return _ENC.decode(kept), len(kept)


class Chunker(Protocol):
    def split(self, docs, doc_id) -> list[Chunk]: ...


def select_chunker(strategy, settings):
    from app.indexing.chunkers.fixed import FixedChunker
    from app.indexing.chunkers.structure import StructureChunker

    if strategy == "fixed":
        return FixedChunker(settings)
    if strategy == "structure":
        return StructureChunker(settings)
    raise ValueError(f"unknown strategy: {strategy}")
