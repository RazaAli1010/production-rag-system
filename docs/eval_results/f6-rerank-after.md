# Eval report — `f6-rerank-after`

- **git SHA:** `b3dbecb61b3c4438997ba95b71fc591473b62579`
- **pipeline flags:** `{"hybrid": true, "rerank": true, "query_rewrite": false, "compression": false, "cache": false, "memory": false}`
- **index manifest:** `{
  "strategy": "fixed",
  "embed_model": "text-embedding-3-small",
  "namespaces": {
    "pu": {
      "vectors": 349,
      "chunks": 349
    },
    "hec": {
      "vectors": 239,
      "chunks": 239
    }
  },
  "total_tokens": 15290,
  "est_cost_usd": 0.0003058,
  "created_at": "2026-07-14T07:55:21.851287+00:00"
}`

## retrieval

| metric | slice | value |
|---|---|---|
| hit@1 | overall | 0.6825 |
| hit@3 | overall | 0.8571 |
| hit@5 | overall | 0.9683 |
| mrr | overall | 0.7907 |
| hit@1 | en | 0.7778 |
| hit@3 | en | 0.9444 |
| hit@5 | en | 0.9722 |
| mrr | en | 0.8620 |
| hit@1 | code_switched | 0.6250 |
| hit@3 | code_switched | 0.7500 |
| hit@5 | code_switched | 0.9375 |
| mrr | code_switched | 0.7312 |
| hit@1 | multi_doc | 0.5000 |
| hit@3 | multi_doc | 1.0000 |
| hit@5 | multi_doc | 1.0000 |
| mrr | multi_doc | 0.7222 |
| hit@1 | table_lookup | 0.5625 |
| hit@3 | table_lookup | 0.8125 |
| hit@5 | table_lookup | 1.0000 |
| mrr | table_lookup | 0.7240 |
