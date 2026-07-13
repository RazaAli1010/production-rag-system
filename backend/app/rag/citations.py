"""`[n]` marker parsing -> `Citation`, resolved via one batched Postgres read (design.md §4,
AC-15/16/17).
"""

import re

from sqlalchemy import select

from app.core.contracts import Citation, RetrievedChunk
from app.core.settings import settings as default_settings
from app.db.models.corpus import Chunk as ChunkRow
from app.db.models.corpus import Document as DocRow
from app.rag.context import extract_quote

_MARKER_RE = re.compile(r"\[(\d+)\]")


def _extract_marker_numbers(text: str, n_chunks: int) -> list[int]:
    """Distinct `[n]` markers, in order of first appearance, dropping any `n` outside
    `1..n_chunks` (AC-17) rather than raising or fabricating a citation."""
    seen: list[int] = []
    for m in _MARKER_RE.finditer(text):
        n = int(m.group(1))
        if 1 <= n <= n_chunks and n not in seen:
            seen.append(n)
    return seen


async def parse_citations(
    answer_text: str, chunks: list[RetrievedChunk], session
) -> list[Citation]:
    """Resolves every distinct valid `[n]` marker to a `Citation` via exactly one batched
    `chunks JOIN documents` read keyed by the chunk_ids actually cited (AC-15) — never a
    per-marker query, never a Pinecone round-trip. `Citation.quote` is always derived from the
    Postgres-stored `chunks.text` (the canonical, never-truncated copy — Pinecone metadata can be
    truncated for oversized chunks per F2's `_build_metadata`), never from the LLM's own text
    (AC-16)."""
    marker_numbers = _extract_marker_numbers(answer_text, len(chunks))
    if not marker_numbers:
        return []

    chunk_ids = [chunks[n - 1].chunk_id for n in marker_numbers]

    stmt = (
        select(ChunkRow, DocRow)
        .join(DocRow, ChunkRow.doc_id == DocRow.doc_id)
        .where(ChunkRow.chunk_id.in_(chunk_ids))
    )
    result = await session.execute(stmt)
    rows = {chunk_row.chunk_id: (chunk_row, doc_row) for chunk_row, doc_row in result.all()}

    citations = []
    for chunk_id in chunk_ids:
        pair = rows.get(chunk_id)
        if pair is None:
            continue  # chunk vanished from Postgres since retrieval — skip, don't crash
        chunk_row, doc_row = pair
        citations.append(
            Citation(
                chunk_id=chunk_row.chunk_id,
                doc_id=doc_row.doc_id,
                title=doc_row.title,
                section_heading=chunk_row.section_heading,
                page_start=chunk_row.page_start,
                page_end=chunk_row.page_end,
                url=doc_row.url,
                quote=extract_quote(chunk_row.text, default_settings.CITATION_QUOTE_MAX_WORDS),
            )
        )
    return citations
