# Eval delta — `f9-cache-after` vs `f8-compression-after`

| metric | slice | f8-compression-after | f9-cache-after | Δ | dir |
|---|---|---|---|---|---|
| cache_cost_saved_mean | overall | 0.0000 | 0.0000 | +0.0000 | ▲ |
| cache_hit_rate | overall | 0.0000 | 0.5000 | +0.5000 | ▲ |
| cost_mean | overall | 0.0000 | 0.0000 | -0.0000 | ▲ |
| latency_cache_hit_p50 | overall | — | 3328.0000 | — | · |
| latency_cache_hit_p95 | overall | — | 3859.0000 | — | · |
| latency_cache_hit_p99 | overall | — | 3859.0000 | — | · |
| latency_cache_lookup_p50 | overall | — | 3530.0000 | — | · |
| latency_cache_lookup_p95 | overall | — | 4625.0000 | — | · |
| latency_cache_lookup_p99 | overall | — | 9390.0000 | — | · |
| latency_p50 | overall | 40437.0000 | 3859.0000 | -36578.0000 | ▲ |
| latency_p95 | overall | 55093.0000 | 51937.0000 | -3156.0000 | ▲ |
| latency_p99 | overall | 157640.0000 | 58844.0000 | -98796.0000 | ▲ |
| latency_searching_p50 | overall | 39171.0000 | 35515.0000 | -3656.0000 | ▲ |
| latency_searching_p95 | overall | 53921.0000 | 47953.0000 | -5968.0000 | ▲ |
| latency_searching_p99 | overall | 156436.0000 | 47953.0000 | -108483.0000 | ▲ |
| tokens_mean | overall | 33.0000 | 16.9667 | -16.0333 | ▲ |

## Notes

- **Hit rate exactly as declared: `cache_hit_rate` 0 → 0.500 ▲.** The workload is 30 requests over 15
  unique questions (`EVAL_LATENCY_REQUESTS=30`, `EVAL_LATENCY_UNIQUE_QUESTIONS=15`), so 0.500 is the
  structural maximum and the cache reached it: **15 entries written, 15 hits, one per entry**. Every
  one of the 15 unique questions produced a cacheable (non-refused, cited) answer.
- **Latency ↓↓ (the headline): p50 40.4 s → 3.9 s (−36.6 s, 10.5×) ▲**, p95 55.1 s → 51.9 s
  (−3.2 s), p99 157.6 s → 58.8 s. p50 is the honest signal here: half the requests are hits, so the
  median request *is* a cache hit. p95/p99 still sit on the miss path (a miss is a full pipeline
  run), which is why they move much less — the cache makes repeat traffic cheap, it does not make
  the pipeline faster.
- **Miss path got FASTER, not slower: `latency_searching_p50` 39.2 s → 35.5 s (−3.7 s) ▲.** This was
  the gate's designated failure signal — a miss-path regression would have meant the `query_vec`
  threading was broken and the pipeline was double-embedding (design §2). It is flat-or-better, as
  predicted: reusing the cache's embedding across the namespace fan-out *removes* 2 redundant embeds
  per request. (Absolute latencies are rate-limit inflated on this tier — see the f8 note below.)
- **`cache_cost_saved_mean` reads 0.0000 — that is display precision, not zero.** The real value is
  **4.241e-05 USD/request** (the table renders %.4f; `cost_mean` = 9.6e-06 rounds away identically,
  and has in every prior report). At a 50% hit rate the cache avoids ~$8.5e-05 of generation spend
  per hit, computed from each cached answer's own `tokens_in`/`tokens_out` via the central
  `estimate_cost` — i.e. the spend the skipped `gpt-4o-mini` call would have incurred.
- **`tokens_mean` 33.0 → 17.0 and `cost_mean` −0.0000 are ARTIFACTS. Do not bank them.** A cache hit
  replays the whole answer as ONE `token` event (AC-24), and `tokens_mean` counts token *events*, so
  it mechanically halves at a 50% hit rate. `cost_mean` is derived from it (output-only), so it
  halves too. Neither reflects a real token or cost reduction — the true saving is
  `cache_cost_saved_mean` above. Both metrics were deliberately left on their pre-F9 basis so this
  delta compares two pipelines rather than two measurements; the cost of that choice is these two
  uninterpretable rows, which is the right trade.

### The `< 300 ms` acceptance criterion — **MISSED, structurally**

`latency_cache_hit_p95` = **3,859 ms**, an order of magnitude over the target. Not a defect, and not
tunable: the cache key IS F7's normalized standalone question, so with `ENABLE_QUERY_REWRITE=on` a
`gpt-4o-mini` rewrite must complete *before* a key can be built. `latency_cache_lookup_p50` = 3,530 ms
is almost entirely that call (measured directly at T15: rewrite alone = 5,065 ms; rewrite off, the
same stage = 1,703 ms, embed-bound).

Only the **Redis exact-match tier** can clear 300 ms — it is the one path that skips both the rewrite
and the embed (design §2's whole reason for checking Redis before embedding). It could not be
measured here: Docker is unavailable on this box, so `REDIS_URL` was `None` and the run used the
Postgres-only tier. **The AC stands unverified rather than passed.** What *is* verified end-to-end
(T15, live): an exact repeat hits with `cache_hit=true`, replays byte-identically, and is **18×**
faster (69.2 s → 3.8 s).

### Verdict — **gate met on every measurable criterion except the 300 ms headline**

`cache_hit_rate` hit its structural maximum, p50 fell 10.5×, the miss path improved, `$ saved` > 0,
and retrieval is untouched. `ENABLE_CACHE` nevertheless ships **default-off** (`false`), for reasons
the latency numbers do not show:

1. **The semantic accept rule's margin is thin.** T7's calibration against real
   `text-embedding-3-small` vectors found the specced rule unusable (nothing reaches 0.95 cosine; the
   adversarial and paraphrase sets *overlap* — worst adversarial pair `15(3)` vs `15(4)` = 0.930 >
   best true paraphrase = 0.912). What ships is `cosine ≥ 0.86 AND discriminative-token agreement`,
   which holds 0 collisions on the committed set but keeps only **2/8** paraphrases with a **0.032**
   margin. See `backend/tests/cache/test_adversarial.py`.
2. **The exact tier is the safe, valuable half; the semantic tier is the risky, marginal half.** This
   run's 15/15 hits were exact repeats of the same rewritten key — they did not exercise the
   thin-margin semantic path at all.
3. Precedent: `ENABLE_QUERY_REWRITE` and `ENABLE_COMPRESSION` both ship default-off under the project
   A/B rule.

**Retrieval hit@k / MRR / RAGAS were NOT re-run**, per CLAUDE.md's "latency/cost suites only" scope
for this gate. The cache is post-retrieval by construction: a hit skips retrieval entirely and a miss
retrieves identically (`query_vec=None` ≡ the pre-F9 path, proved byte-for-byte by
`tests/rag/test_query_vec_threading.py`), so `f8-compression-after`'s retrieval numbers stand
unchanged.

### Provenance / reproducibility

- Both labels ran on git SHA **`374f8155b990`**, same 588-chunk index manifest (`fixed` /
  `text-embedding-3-small`, pu 349 + hec 239), `EVAL_LATENCY_REQUESTS=30`,
  `EVAL_LATENCY_UNIQUE_QUESTIONS=15`, `REDIS_URL` unset (Postgres-only tier).
  f9 flags `hybrid=on,rerank=on,query_rewrite=on,compression=on,cache=on,memory=off`; the f8 baseline
  is the identical config with `cache=off`. Cache flushed cold before each run.
- **`f8-compression-after` was RE-RUN for this gate** (as `f7-rewrite-after` was re-run for F8's —
  same precedent). Two reasons, both mandatory: the DB carried no `eval_runs` row for it
  (`--compare` reads the previous label's row from Postgres), and its committed latency numbers were
  recorded at a different workload (N=30 over 63 answerable records = **zero repeats**, so a cache
  measured against it would have shown a 0% hit rate). The committed
  [`f8-compression-after.md`](f8-compression-after.md) is **unchanged** and still documents the
  original full-suite gate; only the latency rows in the table above come from the re-run.
- **Absolute latencies are not comparable to the original f8 report** (p50 40.4 s here vs 35.0 s
  there; p99 157.6 s vs 39.8 s). The tier was slower today under rate limiting — which is precisely
  why both labels were re-run back-to-back under identical conditions. The **delta** is the signal.
- **The corpus had to be reseeded before this run, and that matters beyond F9.**
  `tests/rag/conftest.py` truncates `chunks`/`documents` in the same database dev and eval runs use,
  and `citations.parse_citations` resolves `[n]` markers by joining those tables. With them empty,
  every answer is `refused=True (no_grounded_claims)` **despite the LLM emitting a correct
  `[1]`-cited answer** — and AC-16 never caches a refusal, so the cache can never write.
  Measured over 6 dataset questions, identical config: **0/6 cacheable empty → 6/6 after reseed.**
  This is what `false_refusal_rate = 1.0000` and 57/75 `refusals_no_grounded_claims` in
  [`f8-compression-after-vs-f7-rewrite-after.md`](f8-compression-after-vs-f7-rewrite-after.md) are
  recording: **those labels were measured on a corpus-less DB.** Reseed with
  `python -m app.ingestion.run --all && python -m app.indexing.run --strategy fixed --namespace all`
  (~$0.0003, no re-download; ingestion must precede indexing or it reports "indexed 0 docs"), and do
  not run pytest between the reseed and the run. `tests/cache/conftest.py` truncates `cache_entries`
  only.
