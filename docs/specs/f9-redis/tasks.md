# F9 — Semantic Cache (Redis + Postgres) · tasks.md

**Module:** `backend/app/caching/` · **Phase:** C · **Depends on:** F7, F12 · **Flag:** `ENABLE_CACHE`
· **Eval gate:** `f9-cache-after` vs `f8-compression-after`

Each task is ≈ ≤ 1 hour and lands green. The order is bottom-up: pure-CPU keys first (they are what
the whole accept rule rests on), then the two storage tiers in isolation, then the vector-reuse
surgery on the retrieval seam, then the splice into `_pipeline_events`, then the CLI, then
calibration, then the gate. Nothing touches `baseline.py` until both tiers pass their own tests —
the pipeline is the last thing to change, not the first.

**T14 IS the feature.** Per CLAUDE.md, F9 is not done when the code works; it is done when
`docs/eval_results/f9-cache-after-vs-f8-compression-after.md` is committed.

---

### T1 — Settings block + Redis dependency
Add the `# --- Semantic cache (F9) ---` block from design §7 to `app/core/settings.py`
(`ENABLE_CACHE`, `REDIS_URL: RedisDsn | None`, `CACHE_REDIS_TTL_S`, `CACHE_REDIS_TIMEOUT_S`,
`CACHE_KEY_PREFIX`, `CACHE_SIMILARITY_THRESHOLD`, `CACHE_LEXICAL_JACCARD_MIN`,
`CACHE_DISCRIMINATIVE_TERMS`, `CACHE_MAX_ENTRIES`), importing `RedisDsn` from `pydantic`. Add
`redis==5.2.1` to `backend/pyproject.toml` under an `# F9` comment block (the first new runtime dep
since F6 — pin exactly, matching the file's convention). Create `backend/tests/cache/` with
`conftest.py` mirroring `tests/rag/conftest.py` (own `engine`/`session`, the `get_engine`/
`get_sessionmaker` `lru_cache` reset, autouse env stubs) — plus `cache_entries` added to the autouse
`TRUNCATE` teardown.

**Test:** `tests/cache/test_settings_schemas.py` — defaults are exactly `ENABLE_CACHE is False`,
`REDIS_URL is None`, threshold `0.95`, jaccard `0.3`, max entries `10_000`, TTL `86_400`; a bad
`REDIS_URL` raises `ValidationError`.

---

### T2 — Alembic `0003`: `query_hash` + `request_id`
Add `query_hash: Mapped[str]` (unique) and `request_id: Mapped[str | None]` to `CacheEntry` in
`app/db/models/ops.py`, then write `0003_f9_cache_entry_keys.py` per design §8 — explicit constraint
names (`uq_cache_entries_query_hash`, `ix_cache_entries_request_id`), a docstring stating what
`0001_initial.py` already created so nothing is recreated, and a symmetric `downgrade()`.

**Test:** extend `tests/db/test_models_ops.py` for the new columns; a `tests/cache/test_migration_0003.py`
in the style of `tests/ingestion/test_migration_0002.py` asserting upgrade → columns+constraint exist,
downgrade → gone, and `alembic revision --autogenerate` yields an empty diff after upgrade (AC-35).

---

### T3 — `app/caching/keys.py`
`normalize`, `exact_key`, `key_terms`, `jaccard` per design §4. Pure CPU, no I/O, no settings arg.
`key_terms` keeps numeric and short-but-discriminating tokens (`bs`, `15(3)`) — filtering them is the
one thing that would break the guard the module exists to power.

**Test:** `tests/cache/test_keys.py` — `normalize` is idempotent and whitespace/case/punctuation
stable; `exact_key` is deterministic and differs for differing normals; `key_terms("what is the bs
admission deadline")` contains `bs`; `jaccard` is 1.0 for identical sets, 0.0 for disjoint, 0.0 for
two empty sets (no ZeroDivisionError).

---

### T4 — `app/caching/redis_hot.py`
`get`/`set`/`flush` on `redis.asyncio` (never the sync client), each wrapped in
`asyncio.timeout(CACHE_REDIS_TIMEOUT_S)` + `except Exception` → `rag.cache_degraded` → return the
miss value. `REDIS_URL is None` short-circuits silently before any client construction (AC-4 — no log
spam). `flush` uses async `SCAN` over `CACHE_KEY_PREFIX + "*"`, never `KEYS`.

**Test:** `tests/cache/test_redis_hot.py` with an injected fake client (F2's DI style, not a mock
library) — `REDIS_URL=None` → `get` returns None and constructs nothing; a client raising
`ConnectionError` → `get` returns None, `set` does not raise, `rag.cache_degraded` logged once
(AC-3); a client that hangs → the timeout fires and returns None; round-trip get/set works; `flush`
deletes only prefixed keys.

---

### T5 — `SemanticCache`: matrix load + cosine lookup
`app/caching/store.py`: the class, `_ensure_loaded` (async DB read of every row → L2-normalized
`float32` matrix + row-parallel `_ids`/`_query_texts`/`_terms`/`_manifests`, under `asyncio.Lock`,
once per process — AC-22), and `lookup` doing the inline matmul and returning
`(AnswerResponse, cosine)` or `None`. Wire the full accept rule: cosine threshold → lexical Jaccard
(`rag.cache_lexical_reject` on rejection, AC-8) → manifest check (miss + delete stale, AC-9). Wrap
the whole body in `except Exception` → `rag.cache_degraded` → `None` (AC-10). Empty matrix → `None`
without a matmul.

**Test:** `tests/cache/test_store.py` (live Postgres, per conftest) — seeded entry + identical vector
hits; a `0.90`-cosine vector misses; a `0.99`-cosine vector with disjoint key terms misses and logs
`cache_lexical_reject`; a stale `index_manifest_id` misses AND the row is gone from both Postgres and
the matrix; a raising sessionmaker returns None rather than propagating; empty cache returns None;
concurrent `asyncio.gather` of two first-lookups loads the matrix exactly once.

---

### T6 — `SemanticCache`: write, evict, flush, delete
`write` (upsert `ON CONFLICT (query_hash) DO UPDATE`, AC-17; append to the in-memory matrix under the
lock), eviction of the least-recently-hit row when `len(_ids) >= CACHE_MAX_ENTRIES` (AC-18), `flush`
(Postgres delete-all + `redis_hot.flush` + matrix clear), `delete_by_request_id` (AC-21), plus the
`hits += 1` / `last_hit_at = now()` update on a hit. Then the module-level seam: `lookup(...)` and
`schedule_write(...)` — `asyncio.create_task` with the strong-reference set and a
`task.add_done_callback(_tasks.discard)` (AC-14), the task body wrapped in `except Exception` →
`rag.cache_write_failed` (AC-19).

**Test:** same file — write then lookup round-trips the `AnswerResponse` (citations included) intact;
writing the same normalized query twice leaves exactly one row with the second answer; at
`CACHE_MAX_ENTRIES=2` a third write evicts the least-recently-hit and the matrix has 2 rows; a hit
increments `hits` and sets `last_hit_at`; `flush` returns the count and empties both tiers;
`delete_by_request_id` returns 1 then 0; a `schedule_write` whose write raises logs
`cache_write_failed` and does not leave an unretrieved-exception warning.

---

### T7 — Calibrate the accept rule against the adversarial set
Commit `backend/tests/fixtures/cache/adversarial.jsonl` — pairs labelled `should_hit` (true
paraphrases, incl. code-switched: "probation se kaise nikalta hoon" / "how do I get off academic
probation") and `should_miss` (near-identical syntax, different answer: BS vs MPhil admission
deadline; 2023 vs 2024 fee schedule; PU vs HEC plagiarism penalty; section 15(3) vs 15(4)). Embed both
sides with the real `text-embedding-3-small`, record the cosine and Jaccard for every pair, and find
the `CACHE_SIMILARITY_THRESHOLD` / `CACHE_LEXICAL_JACCARD_MIN` pair that separates the two sets. If no
pair separates them, implement the `CACHE_DISCRIMINATIVE_TERMS` fallback rule (design §5). Record the
shipped numbers and the measured margin in a comment on the Settings keys — the same tuned-not-guessed
discipline `REFUSAL_RERANK_THRESHOLD` got at the F6 gate.

**Test:** `tests/cache/test_adversarial.py` (committed, offline — vectors cached into the fixture, no
live OpenAI in CI) — every `should_miss` pair does NOT collide at the shipped thresholds and every
`should_hit` pair DOES. This is feature-level AC #2 and the reason this task precedes the pipeline
splice: if the rule cannot be defended, the cache ships default-off and the design says so.

---

### T8 — `retriever.embed_query` + `query_vec` threading
Add `embed_query(query, settings)` (`.aembed_query` — the async surface; the grep guard rejects
`.embed_query(`) reusing `_build_store`'s `OpenAIEmbeddings`. Thread `query_vec=None` through
`_retrieve_namespace` → `dense_retrieve` → `gather_candidate_pool` → `retrieve`; when present,
`_retrieve_namespace` calls `asimilarity_search_by_vector_with_score(query_vec, k=..., namespace=...)`
instead of `asimilarity_search_with_score`. `None` must restore the byte-for-byte current path
(AC-13).

**Test:** extend `tests/rag/test_retriever.py` + `test_pinecone_vectorstore_async.py` with an injected
fake store — `query_vec=None` → `asimilarity_search_with_score` called, by-vector never; `query_vec`
given → `asimilarity_search_by_vector_with_score` called with that exact vector, once per namespace,
and the by-query surface never called; both return identical `RetrievedChunk` shapes.

---

### T9 — `rewrite.retrieve(..., rr=..., query_vec=...)` — no double rewrite
Add `rr=None` and `query_vec=None` to `rewrite.retrieve`; when `rr` is supplied, skip `rewrite_query`
and go straight to `multi_query_retrieve` (still stashing it via `_REWRITE_RESULT` so
`last_rewrite()` keeps working — AC-12). Thread `query_vec` through `multi_query_retrieve` to
`gather_candidate_pool` **only** for the fan-out query equal to `rr.normalized`; variants embed
themselves. Add `"ENABLE_CACHE": flags.cache` to `rag/flags.apply_flags` (AC-31).

**Test:** extend `tests/rag/test_rewrite.py` — passing `rr` calls the rewrite LLM zero times and
`multi_query_retrieve` once; omitting it preserves today's behaviour exactly; `query_vec` reaches
`gather_candidate_pool` for the normalized query and is `None` for both variants;
`apply_flags(s, PipelineFlags(cache=True)).ENABLE_CACHE is True`.

---

### T10 — `observability.log_cache` + the pipeline splice
Add `log_cache(...)` to `app/rag/observability.py` per design §4 — a structlog emit mirroring
`log_rerank`/`log_rewrite`/`log_compression`, with `est_cost_saved_usd` computed by the caller via
F2's central `estimate_cost` over the cached response's `tokens_in`/`tokens_out` (AC-26/27). Then
splice the seam into `_pipeline_events` per design §3, entirely under `if settings.ENABLE_CACHE`:
`cache_lookup started` → rewrite-once → Redis exact → embed → semantic → hit-replay or fall through
with `rr`/`query_vec`. Extend the CI sync-twin grep (`.github/workflows/ci.yml:261`) and the in-suite
guard (`tests/rag/test_generation.py:52-64`) to glob `app/caching/*.py` (AC-29).

**Test:** extend `tests/rag/test_streaming.py` — `ENABLE_CACHE=False` emits no `cache_lookup` stage
and the event sequence is identical to today (the toggle-parity test, AC-30); with the cache on and a
seeded hit the order is exactly `cache_lookup started/done` → three `skipped` stages → one `token` →
`citations` → `meta(cache_hit=True)` → `done` (AC-24), the retriever and LLM are never called, and
`astream`/`answer` agree on the reassembled text byte-for-byte including the disclaimer (AC-25); the
grep guard test passes over `app/caching/`.

---

### T11 — Write-behind wiring
Call `store.schedule_write(...)` in `_pipeline_events` **after** the terminal `done` event, gated on
`not refused and not degraded and citations and not cache_hit` (AC-16). Confirm the `done` event's
timing is independent of the write (AC-15).

**Test:** `tests/cache/test_acceptance.py` — a successful answer schedules exactly one write and the
entry is present after the task drains; a refused answer, a `degraded=True` answer, a zero-citation
answer, and a cache-hit answer each schedule zero writes; a write that sleeps 200ms does not delay the
`done` event (assert `done` is yielded before the task completes).

---

### T12 — `app/caching/run.py` CLI
`python -m app.caching.run --flush` and `--delete-request-id <id>`, in `app/evals/run.py`'s style:
`argparse`, injectable `settings`/`sessionmaker` for tests, `async def main(argv=None, ...) -> int`,
and `_entrypoint()` = `raise SystemExit(asyncio.run(main()))`. `--flush` prints the deleted count and
exits 0 (AC-20); `--delete-request-id` exits 0 on a match, 1 on none (AC-21); neither flag → usage,
exit 2.

**Test:** `tests/cache/test_run.py` — `main(["--flush"], settings=..., sessionmaker=...)` returns 0
and empties both tiers; `--delete-request-id` returns 0 then 1; no args returns 2. Redis being down
must not make `--flush` fail its Postgres half.

---

### T13 — Eval harness: let latency (and only latency) see the cache
`parse_flags(spec, *, allow_cache=False)` per design §9 — force `cache=False` unless `allow_cache`
(AC-32). `run.py` passes `allow_cache=(expand_suites(args.suite) == ["latency"])` (AC-33).
`latency.py:121`'s hardcoded `"skip_cache": True` → `not flags.cache`. Emit the new metrics from
`run_latency` under the exact names design §9 fixes: `cache_hit_rate`, `cache_cost_saved_mean`,
`latency_cache_hit_p50` / `_p95` (harvested from `meta.cache_hit`). `compare.py` needs **no** change —
the names carry the direction.

**Test:** extend `tests/evals/test_flags.py` — `parse_flags("cache=on")` → `cache is False`;
`parse_flags("cache=on", allow_cache=True)` → `True`; `--suite all --flags cache=on` forces False
end-to-end through `run.main`; `--suite latency --flags cache=on` does not. Extend
`tests/evals/test_latency.py` — an injected `astream` yielding `cache_hit=True` produces
`cache_hit_rate` and `latency_cache_hit_p95`; `test_compare.py` asserts `cache_hit_rate` renders ▲ on
an increase and `latency_cache_hit_p95` renders ▲ on a decrease.

---

### T14 — Acceptance / definition of done
Run the full acceptance set against a live Postgres + Redis (`make db-up && make migrate`).

**Definition of done:** every criterion in requirements §4 is green —
1. paraphrase hit `< 300ms` end-to-end; exact repeat hits with zero embed calls;
2. adversarial set does not collide at shipped thresholds (T7's committed test);
3. hit rate / tokens saved / $ saved logged on every lookup;
4. Redis down → still answering, `rag.cache_degraded`, no 5xx;
5. stale manifest → not served, entry deleted;
6. `--flush` and `--delete-request-id` work live;
7. `ENABLE_CACHE=false` is byte-for-byte `f8-compression-after` (toggle-parity test);
8. retrieval/RAGAS/refusal runs force `cache=False`;
9. `0003` upgrades + downgrades clean, autogenerate diff empty;
10. the `app/caching/*.py` grep guard passes in CI.

---

### T15 — EVAL GATE (mandatory Phase-C closer)
Run the F4 harness against the cache path and commit the delta reports. Per CLAUDE.md, F9's gate is
**latency/cost suites only** — the cache is post-retrieval by construction (a hit skips retrieval
entirely; a miss retrieves identically), so hit@k / MRR / RAGAS are not re-measured and
`f8-compression-after`'s retrieval numbers stand.

```bash
# Same dense index + bm25.pkl as f8-compression-after (same SHA/manifest); F9 adds no re-index and
# no re-embed of the corpus — the cache stores answers, not chunks (design §10).
#
# The workload is repeat-heavy by construction: run_latency samples answerable[i % len(answerable)],
# so with EVAL_LATENCY_REQUESTS > len(answerable) the first pass populates the cache and every later
# pass is an exact repeat (design §9). Flush first so the run starts cold and the hit rate is a known
# function of N, not of whatever was cached yesterday.
python -m app.caching.run --flush

python -m app.evals.run --suite latency \
    --flags hybrid=on,rerank=on,query_rewrite=on,compression=on,cache=on,memory=off \
    --label f9-cache-after --yes

python -m app.evals.run --label f9-cache-after --compare f8-compression-after
```

**Match N to the f8 baseline.** `f8-compression-after`'s latency suite was trimmed to 30 requests; the
`f8` row in the delta must come from a run at the same `EVAL_LATENCY_REQUESTS`, or the comparison is
between two different workloads. If the committed `f8-compression-after` label used a different N,
re-run it at the F9 N with `cache=off` before comparing, and say so in Provenance.

Then commit `docs/eval_results/f9-cache-after.md` and
`docs/eval_results/f9-cache-after-vs-f8-compression-after.md`.

**Definition of done (the gate):** the delta reports exist and are committed, mapping
`f9-cache-after` → its git SHA + index manifest. Per the `docs/eval_results/` Notes convention
(headline vs target → distribution/mechanism → each regression named with its tradeoff → explicit
**Verdict** → **Provenance**), the report shows:
- **`cache_hit_rate` reported** against the workload's structural expectation
  (`1 - len(answerable)/N`) — a materially lower rate means entries are not being written or the
  accept rule is too tight, and must be explained, not averaged away;
- **`latency_cache_hit_p95` < 300ms** — the feature's headline acceptance criterion;
- **`latency_p50` / `latency_p95` down** vs `f8-compression-after`, with the miss-path p95 noted
  separately (it must be **flat or slightly better** — design §2's vector reuse removes 2 redundant
  namespace embeds; a miss-path *regression* means the vector threading is broken, and is a gate
  failure regardless of how good the hit numbers look);
- **`cache_cost_saved_mean` > 0** and cost/query down;
- **`tokens_mean` noted** (hits replay cached `tokens_out`, so this measures the cache, not the LLM);
- **retrieval hit@k / MRR / RAGAS not re-run** — stated explicitly, with the reason above.

**Ship default-off unless the numbers earn otherwise.** `ENABLE_CACHE` defaults to `false` and stays
there if T7's threshold margin is thin or the hit-rate/latency win does not materialize —
`ENABLE_QUERY_REWRITE` already set this precedent at the F7 gate. The gate is a measurement, not a
formality.

Per CLAUDE.md, **the feature is not done until this delta table is committed.**

---

**Gate label sequence (fixed):** `baseline` → `f5-hybrid-after` → `f6-rerank-after` →
`f7-rewrite-after` → `f8-compression-after` → **`f9-cache-after`** → `f17-memory-after`. F9's "before"
is the `f8-compression-after` report; F17's "before" will be `f9-cache-after`. Latency/cost suites only
for the last two. Every README benchmark row for the cache maps to the `f9-cache-after` label, which
maps to a git SHA + index manifest, so all numbers are reproducible.
