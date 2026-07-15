# Eval report — `f8-compression-after`

- **git SHA:** `e9ec591d10937d79b0bdcb348226a29acd835312`
- **pipeline flags:** `{"hybrid": true, "rerank": true, "query_rewrite": true, "compression": true, "cache": false, "memory": false}`
- **index manifest:** `{"strategy": "fixed", "embed_model": "text-embedding-3-small", "namespaces": {"pu": {"vectors": 349, "chunks": 349}, "hec": {"vectors": 239, "chunks": 239}}, "total_tokens": 15290, "est_cost_usd": 0.0003058, "created_at": "2026-07-14T07:55:21.851287+00:00"}`

## retrieval

| metric | slice | value |
|---|---|---|
| hit@1 | overall | 0.6667 |
| hit@3 | overall | 0.8730 |
| hit@5 | overall | 0.9524 |
| mrr | overall | 0.7802 |
| hit@1 | en | 0.7778 |
| hit@3 | en | 0.9444 |
| hit@5 | en | 0.9722 |
| mrr | en | 0.8620 |
| hit@1 | code_switched | 0.5625 |
| hit@3 | code_switched | 0.7500 |
| hit@5 | code_switched | 0.8750 |
| mrr | code_switched | 0.6740 |
| hit@1 | multi_doc | 0.3333 |
| hit@3 | multi_doc | 1.0000 |
| hit@5 | multi_doc | 1.0000 |
| mrr | multi_doc | 0.6111 |
| hit@1 | table_lookup | 0.5625 |
| hit@3 | table_lookup | 0.8750 |
| hit@5 | table_lookup | 1.0000 |
| mrr | table_lookup | 0.7396 |

## ragas

| metric | slice | value |
|---|---|---|
| faithfulness | overall | 0.8810 |
| answer_relevancy | overall | 0.5578 |
| context_precision | overall | 0.6276 |
| context_recall | overall | 0.7738 |

## refusal

| metric | slice | value |
|---|---|---|
| refusal_recall | overall | 1.0000 |
| false_refusal_rate | overall | 1.0000 |
| refusals_low_retrieval_confidence | overall | 18.0000 |
| refusals_no_grounded_claims | overall | 57.0000 |

## latency

| metric | slice | value |
|---|---|---|
| latency_p50 | overall | 35000.0000 |
| latency_p95 | overall | 38187.0000 |
| latency_p99 | overall | 39781.0000 |
| latency_searching_p50 | overall | 33000.0000 |
| latency_searching_p95 | overall | 35328.0000 |
| latency_searching_p99 | overall | 35561.0000 |
| tokens_mean | overall | 35.1000 |
| cost_mean | overall | 0.0000 |
