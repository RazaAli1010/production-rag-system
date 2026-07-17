# Eval report — `f9-cache-after`

- **git SHA:** `374f8155b99047d3271dd51f05880e4a6aacb7b6`
- **pipeline flags:** `{"hybrid": true, "rerank": true, "query_rewrite": true, "compression": true, "cache": true, "memory": false}`
- **index manifest:** `{"strategy": "fixed", "embed_model": "text-embedding-3-small", "namespaces": {"pu": {"vectors": 349, "chunks": 349}, "hec": {"vectors": 239, "chunks": 239}}, "total_tokens": 15290, "est_cost_usd": 0.0003058, "created_at": "2026-07-17T10:27:33.563718+00:00"}`

## latency

| metric | slice | value |
|---|---|---|
| latency_p50 | overall | 3859.0000 |
| latency_p95 | overall | 51937.0000 |
| latency_p99 | overall | 58844.0000 |
| latency_cache_lookup_p50 | overall | 3530.0000 |
| latency_cache_lookup_p95 | overall | 4625.0000 |
| latency_cache_lookup_p99 | overall | 9390.0000 |
| latency_searching_p50 | overall | 35515.0000 |
| latency_searching_p95 | overall | 47953.0000 |
| latency_searching_p99 | overall | 47953.0000 |
| tokens_mean | overall | 16.9667 |
| cost_mean | overall | 0.0000 |
| cache_hit_rate | overall | 0.5000 |
| latency_cache_hit_p50 | overall | 3328.0000 |
| latency_cache_hit_p95 | overall | 3859.0000 |
| latency_cache_hit_p99 | overall | 3859.0000 |
| cache_cost_saved_mean | overall | 0.0000 |
