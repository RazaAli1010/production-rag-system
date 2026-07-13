"""F1 — multi-format ingestion pipeline (design.md).

Turns `data/sources.csv` into clean, versioned, citation-anchored text in
`data/extracted/{doc_id}.jsonl`, plus a `documents` row per source. Does not chunk, embed, or
index (F2's job).
"""
