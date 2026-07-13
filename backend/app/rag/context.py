"""Numbered context assembly + deterministic quote extraction (design.md §4, AC-10/AC-16)."""

from app.core.contracts import RetrievedChunk


def _format_location(chunk: RetrievedChunk) -> str:
    if chunk.page_start is not None:
        if chunk.page_end is not None and chunk.page_end != chunk.page_start:
            return f"p. {chunk.page_start}-{chunk.page_end}"
        return f"p. {chunk.page_start}"
    if chunk.anchor:
        return chunk.anchor
    return ""


def format_context(chunks: list[RetrievedChunk]) -> str:
    """1-indexed numbered blocks, in `retrieve()`'s order, so the LLM's `[n]` markers map 1:1
    onto `chunks` (AC-10). Each header carries title / section heading / page-or-anchor."""
    blocks = []
    for i, chunk in enumerate(chunks, start=1):
        header = f"[{i}] {chunk.title}"
        if chunk.section_heading:
            header += f" — {chunk.section_heading}"
        location = _format_location(chunk)
        if location:
            header += f" ({location})"
        blocks.append(f"{header}\n{chunk.text}")
    return "\n\n".join(blocks)


def extract_quote(text: str, max_words: int) -> str:
    """Deterministic ≤`max_words`-word extraction from stored chunk text (AC-16) — never
    LLM-authored, so a quote can never be hallucinated or exceed the limit. Splits on
    whitespace, so truncation always lands on a word boundary, never mid-word."""
    words = text.split()
    if len(words) <= max_words:
        return text.strip()
    return " ".join(words[:max_words])
