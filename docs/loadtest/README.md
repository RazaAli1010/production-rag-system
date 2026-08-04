# Load-test reports

One report per run: `<git-sha>-50u.md`, with the raw Locust CSVs under `raw/`.

**No run is committed yet.** The 50-user run is measured against the *deployed* API (F15 T17), and
deployment provisioning — Render, Neon/Supabase, Upstash, Vercel — has not been done yet. A run
against a laptop would measure the laptop, not the deployment, so publishing one would be worse
than publishing nothing.

The harness itself is committed and verified against the local stack: see
[`loadtest/`](../../loadtest/README.md).

## What a report must contain

Anything less and the numbers are not interpretable:

| Field | Why it must be stated |
|---|---|
| git SHA + image tag | which build produced the numbers |
| instance tier + `WEB_CONCURRENCY` | free-tier 512MB and 1 worker is a different system from 2GB and 4 |
| `ENABLE_*` flag configuration | rewrite on triples retrieval; cache on changes p50 by ~10× |
| rate-limit configuration used | 50 users against `RATE_LIMIT_ANON_PER_MIN=5` measures the limiter |
| authed / anonymous split | different tiers, different code paths |
| Locust p50 / p95 / p99, RPS, failure rate | client-side truth |
| TTFT p50 / p95 | what a streaming UI actually feels like |
| `429` and `409 session_busy` counts | a high share of either invalidates the run |
| server-side cache hit rate | from `request_logs`, not inferred from latency |
| `avg(retrieve_ms)`, `avg(rerank_ms)`, `avg(llm_ms)` | the bottleneck analysis, if p95 misses 4s |

## Gate

p95 < 4s at 50 users cache-warm, **or** a bottleneck analysis naming the dominant stage from the
per-stage `request_logs` columns and the remediation considered. Both outcomes are acceptable; a
missing analysis is not.
