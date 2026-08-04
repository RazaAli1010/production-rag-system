# Load test (F15)

50 concurrent users against `POST /api/ask`, measured through the SSE stream rather than at the
handshake.

```bash
pip install -e "backend[load]"

# against the local stack
LOAD_HOST=http://localhost:8000 make load

# against the deployed API, with a mix of authenticated users
LOAD_EMAIL=student@pu.edu.pk LOAD_PASSWORD=... \
LOAD_HOST=https://your-api.onrender.com make load
```

Raw CSVs land in `docs/loadtest/raw/<sha>*`; the write-up goes in `docs/loadtest/<sha>-50u.md`.

## What it does

| Cohort | Weight | Behaviour |
|---|---|---|
| `SingleTurnUser` | 7 | one stateless ask, then think-time |
| `SessionUser` | 3 | `POST /api/sessions` then **three sequential** asks reusing `session_id` |

- **80% repeat / 20% novel** questions, drawn from the F4 eval dataset so the corpus can actually
  answer them. Novel questions carry a nonce so they can never hit either cache tier.
- **Half the users authenticate** (when `LOAD_EMAIL`/`LOAD_PASSWORD` are set), exercising both
  rate-limit tiers.
- Session turns are sequential because F17 serialises a session behind an `asyncio.Lock` — a
  parallel client would measure the lock, not the pipeline.

## Reading the output

Locust reports two request types per ask:

- `POST /api/ask [...]` — full time to the terminal `done` frame. **This is the p95 the gate is
  about.**
- `TTFT /api/ask` — time to the first `token` frame; what a streaming UI actually feels like.

Plus three tags, which are counted but never failed:

- `cache hit` — from the `meta` frame's `cache_hit`. Cross-check against `request_logs`.
- `429 rate-limited` — the limiter working.
- `409 session_busy` — F17's per-session lock.

## Two ways to get a meaningless number

1. **Not consuming the stream.** Locust's default `catch_response=False` marks a streaming request
   successful when response *headers* arrive, which for SSE is near-instant. The p95 you would
   publish is "how fast the server accepts a TCP connection". `locustfile.py` drains every stream
   to `done` and fails any that ends without one.
2. **Being rate-limited.** 50 users against `RATE_LIMIT_ANON_PER_MIN=5` is a 429 storm, and a p95
   achieved by being rejected is not a p95. Either raise the anon tier for the run or run mostly
   authenticated — **and say which in the report**. If the `429` tag is a large share of requests,
   the run is invalid.

Likewise, a `409 session_busy` rate above ~1% means the session cohort's think-time is too short
and it is measuring lock contention.

## Server-side numbers

Locust gives client latency, RPS and error rate. Cache hit rate and per-stage attribution come from
the server over the run window — every column already exists (F13), so the load test adds no
logging:

```sql
SELECT count(*),
       avg(cache_hit::int)                                        AS cache_hit_rate,
       percentile_cont(0.50) WITHIN GROUP (ORDER BY total_ms)     AS p50,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY total_ms)     AS p95,
       percentile_cont(0.99) WITHIN GROUP (ORDER BY total_ms)     AS p99,
       avg(retrieve_ms), avg(rerank_ms), avg(llm_ms),
       sum(est_cost_usd)
FROM request_logs
WHERE ts BETWEEN :start AND :end;
```

`GET /internal/stats?window=1h` (admin) returns the same aggregation without SQL.

If p95 misses the 4s target, those three `avg(*_ms)` columns **are** the bottleneck analysis — which
is why the report table is built from the per-stage columns, not just `total_ms`.
