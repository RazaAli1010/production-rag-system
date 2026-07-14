"""Pre-LLM confidence gate + post-LLM zero-citation gate (design.md §4/§7, AC-6..9, AC-18)."""

from app.core.contracts import Citation, RetrievedChunk
from app.core.settings import settings as default_settings
from app.rag.context import extract_quote


def pre_llm_gate(chunks: list[RetrievedChunk], settings) -> bool:
    """True when the system should refuse *before* invoking the LLM (AC-6). Empty retrieval is
    treated as `dense_score = -inf` — the same refusal path, no special case (design.md §7).

    F5 (AC-15): evaluates the **maximum** `dense_score` across the retrieved set, not
    `chunks[0].dense_score`. After RRF fusion the top chunk may be a strong sparse-only hit
    (`dense_score is None`); ranking it #1 must not spuriously refuse an in-corpus query when a
    dense chunk above threshold sits deeper in the fused pool. A set whose every dense_score is
    below threshold (or absent) is still refused — out-of-corpus protection intact. For the F3
    dense-only path the max is `chunks[0]`, so behaviour is unchanged there."""
    if not chunks:
        return True
    dense_scores = [c.dense_score for c in chunks if c.dense_score is not None]
    top_score = max(dense_scores) if dense_scores else float("-inf")
    return top_score < settings.REFUSAL_DENSE_THRESHOLD


def suggestion_citations(chunks: list[RetrievedChunk], n: int) -> list[Citation]:
    """Up to `n` "you might check" suggestions, one per distinct `doc_id`, drawn straight from
    the already-retrieved set — no extra retrieval, no DB round-trip (AC-7). `url` is left unset
    (see `Citation.url` docstring in contracts.py); `quote` still previews the chunk text so the
    suggestion is useful even though no claim is being grounded."""
    seen_doc_ids: set[str] = set()
    suggestions: list[Citation] = []
    for chunk in chunks:
        if chunk.doc_id in seen_doc_ids:
            continue
        seen_doc_ids.add(chunk.doc_id)
        suggestions.append(
            Citation(
                chunk_id=chunk.chunk_id,
                doc_id=chunk.doc_id,
                title=chunk.title,
                section_heading=chunk.section_heading,
                page_start=chunk.page_start,
                page_end=chunk.page_end,
                quote=extract_quote(chunk.text, default_settings.CITATION_QUOTE_MAX_WORDS),
            )
        )
        if len(suggestions) >= n:
            break
    return suggestions


def post_llm_gate(citations: list[Citation]) -> bool:
    """True when a would-be non-refusal answer has zero valid grounded citations — the caller
    converts this to `refused=True, refusal_reason="no_grounded_claims"` (AC-18)."""
    return len(citations) == 0
