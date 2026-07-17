# Eval results (F4)

Auto-generated reports from the F4 harness live here — one `{label}.md` per run and one
`{label}-vs-{prev}.md` per `--compare`. Each Phase B/C feature commits its delta report here as its
eval-gate artifact (fixed label sequence: `baseline → f5-hybrid-after → f6-rerank-after →
f7-rewrite-after → f8-compression-after → f9-cache-after → f17-memory-after`).

## Dataset status — VERIFIED (75 records)

`backend/app/data/evals/qa_dataset.jsonl` ships **75 records** authored and verified against the live
ingested corpus, and **passes `python -m app.evals.run --lint`** at the production thresholds (60–80
records, ≥15 `code_switched`, ≥10 `out_of_corpus`).

Tag distribution: `en` 36 · `code_switched` 20 · `table_lookup` 16 · `multi_doc` 6 · `out_of_corpus`
12. Every non-`out_of_corpus` record cites a real `doc_id` + page number whose answer was checked
against the extracted document text.

Corpus (registered in `backend/app/data/sources.csv`, ingested via F1 → indexed via F2):

| doc_id | org | source |
|---|---|---|
| `pu-semester-rules-ug` | PU | Punjab University Semester Rules & Regulations |
| `pu-semester-rules-affiliated` | PU | PU Semester Rules (Affiliated Colleges) |
| `pu-fee-schedule-ug-2024` | PU | PU Fee/Dues Schedule (Undergraduate) FY2024-25 |
| `pu-fee-schedule-grad-2024` | PU | PU Fee/Dues Schedule (MS/MPhil/PhD) FY2024-25 |
| `hec-plagiarism-policy-2021` | HEC | HEC Plagiarism Policy (juw.edu.pk mirror; hec.gov.pk is SSL-blocked here) |

To reproduce the corpus: `python -m app.ingestion.run --all` then
`python -m app.indexing.run --strategy fixed --namespace all`. (On Windows set `PYTHONUTF8=1` so
structlog can print the Urdu/code-switched content without a cp1252 crash.)

## Recording the `baseline` label (needs live OpenAI/Pinecone keys + an ingested index)

```bash
cd backend
python -m app.evals.run --suite all --label baseline --yes     # writes baseline.md + DB rows
python -m app.evals.run --label baseline --compare baseline    # sanity: all-zero delta
```

`--yes` confirms the RAGAS judge spend (a cost preview prints first). Every run stamps the report
with the git SHA + index manifest so numbers are reproducible.

## F5 — Hybrid search (BM25 + dense + RRF) · gate: `f5-hybrid-after` vs `baseline`

Delta report: [`f5-hybrid-after-vs-baseline.md`](f5-hybrid-after-vs-baseline.md) (retrieval suite,
same index as `baseline`, `--flags hybrid=on`). Reproduce:

```bash
cd backend
python -m app.evals.run --suite retrieval --label baseline                       # dense-only
python -m app.evals.run --suite retrieval --flags hybrid=on --label f5-hybrid-after
python -m app.evals.run --label f5-hybrid-after --compare baseline
```

**Result (overall):** hit@1 **0.619 → 0.683 (+0.064 ▲)**, MRR **0.772 → 0.787 (+0.015 ▲)**, but
hit@3 −0.016 ▼ and hit@5 **0.984 → 0.921 (−0.064 ▼)**. Unweighted RRF at generation-`k`=5 (no
reranking yet) promotes strong sparse hits to the top — lifting precision@1 (the intended exact-term
rescue, e.g. `table_lookup` hit@1 **+0.125 ▲**) while displacing some dense chunks out of the top-5,
costing recall. This precision-for-recall trade is expected to be recovered by **F6 reranking**,
which re-orders the 12-candidate fused pool instead of truncating it. `ENABLE_HYBRID` defaults
**off**, so this ships dark and is A/B-gated.

> The `ragas` / `refusal` / `latency` suites are not part of F5's gate (per the CLAUDE.md
> label-sequence note, latency/cost suites attach only to `f9-cache-after` / `f17-memory-after`);
> F5's gate is the retrieval hit@k / MRR delta above.

## F6 — Cross-encoder reranking · gate: `f6-rerank-after` vs `f5-hybrid-after`

Delta report: [`f6-rerank-after-vs-f5-hybrid-after.md`](f6-rerank-after-vs-f5-hybrid-after.md)
(retrieval suite, same index as `f5-hybrid-after`, `--flags hybrid=on,rerank=on`). The cross-encoder
(`cross-encoder/ms-marco-MiniLM-L-6-v2`) reranks F5's 12-candidate fused pool → top-5. Reproduce:

```bash
cd backend
python -m app.evals.run --suite retrieval --flags hybrid=on --label f5-hybrid-after
python -m app.evals.run --suite retrieval --flags hybrid=on,rerank=on --label f6-rerank-after
python -m app.evals.run --label f6-rerank-after --compare f5-hybrid-after
```

**Result (overall):** reranking **recovers the recall F5 traded away** — hit@5 **0.921 → 0.968
(+0.048 ▲)** (baseline was 0.984; F5's unweighted-RRF truncation had dropped it to 0.921, and F6
re-orders the 12-pool instead of truncating it, as F5's note predicted). MRR **0.787 → 0.791
(+0.004 ▲)**, hit@1 flat. hit@3 dips −0.048 ▼ (the cross-encoder reshuffles ranks 2–3). By slice the
picture is nuanced and honest:

- **`en` ▲** — hit@1 **+0.083**, MRR **+0.045** (prose relevance is exactly what the model is trained
  for).
- **`code_switched` mixed→▲** — hit@1 **+0.063**, hit@5 **+0.125**, MRR **+0.065**, but hit@3 −0.063;
  the English cross-encoder helps at 1/5 despite scoring Roman-Urdu pairs weakly.
- **`table_lookup` ▼** — hit@1 **−0.125**, MRR **−0.057** (hit@5 +0.125); the prose-trained model
  mis-ranks tabular/numeric content. This regression is the reason F6 **ships default-off**
  (`ENABLE_RERANK=false`) and is A/B-gated — flip it per request/env only where it wins.

**Refusal-gate calibration (`REFUSAL_RERANK_THRESHOLD`).** F6 replaces the v1 dense-cosine gate with
the calibrated `max_rerank_score`. Tuned on the 75-record set: out-of-corpus queries all score
**≤ 0.005** (the model finds no relevant chunk), so the Youden-optimal **0.01** refuses **100%** of
out-of-corpus while still answering **~86%** of in-corpus. A higher value (e.g. 0.5) would
over-refuse — the prose-trained model scores in-corpus *code-switched* queries near 0.

> Same as F5, the `ragas` / `refusal` / `latency` suites are not part of F6's gate; F6's gate is the
> retrieval hit@k / MRR delta above. `rerank_ms` p50 stays well under the 300 ms budget (CPU,
> 12 pairs, single batched `score` call off the event loop).

## F8 — Context compression · gate: `f8-compression-after` vs `f7-rewrite-after`

Delta report: [`f8-compression-after-vs-f7-rewrite-after.md`](f8-compression-after-vs-f7-rewrite-after.md)
(`--suite all`, same 588-chunk index as `f7-rewrite-after`, `--flags hybrid=on,rerank=on,query_rewrite=on,compression=on`).
Post-rerank / pre-generation compression drops low-relevance + duplicate context and sentence-trims
the one budget-overflow chunk, reusing the F6 cross-encoder (no new model call). Reproduce:

```bash
cd backend && set -a && . ./.env && set +a   # RAGAS/generation read OPENAI_API_KEY from the env
EVAL_LATENCY_REQUESTS=30 python -m app.evals.run --suite all \
    --flags hybrid=on,rerank=on,query_rewrite=on,compression=off,memory=off --label f7-rewrite-after --yes
EVAL_LATENCY_REQUESTS=30 python -m app.evals.run --suite all \
    --flags hybrid=on,rerank=on,query_rewrite=on,compression=on,memory=off  --label f8-compression-after --yes
python -m app.evals.run --label f8-compression-after --compare f7-rewrite-after
```

**Result (overall):** **22.8% mean context-token reduction** (aggregate over 143 compressed
generations, from the `rag.compression` logs) with **faithfulness UP 0.868 → 0.881 (+0.013 ▲)** —
removing filler shrinks the ungrounded-claim surface. **context_precision flat** (−0.001),
**retrieval hit@k / MRR unchanged** (Δ = 0 — compression is post-retrieval), **p95 latency −2.8 s**.
Token reduction lands just under the 25% aspiration → **documented, not re-tuned**: the tradeoff is
**answer_relevancy −0.080 ▼** (fewer chunks → slightly less complete answers), so a more aggressive
floor to chase the last 2 points would deepen that regression. Ships **default-off**
(`ENABLE_COMPRESSION=false`), A/B-gated; the faithfulness + latency wins make it safe to enable.

> Absolute latencies (~38–41 s p95) are inflated by OpenAI rate-limiting on the test tier; the
> **delta** is the comparable signal. `EVAL_RAGAS_MAX_WORKERS=4` caps the judge fan-out (the library
> default storms a rate-limited tier into a stall). The `f7-rewrite-after` baseline was re-run to add
> the ragas/latency rows (the prior commit was retrieval-only) — see the delta's provenance note.

## ⚠️ Before ANY eval run: reseed the corpus

`tests/rag/conftest.py` (and `tests/indexing`, `tests/ingestion`) truncate `chunks`/`documents` in
the **same database** dev and eval runs use — there is no separate test DB. `citations.
parse_citations` resolves every `[n]` marker by joining those tables, so once they are empty **every
answer is refused (`no_grounded_claims`) even though the LLM emitted a correct `[1]`-cited answer.**
Retrieval still works (Pinecone is untouched), so hit@k looks fine while every generated answer is
discarded.

Measured 2026-07-17 over 6 dataset questions, identical config: **0/6** answers cited with the tables
empty → **6/6** after reseeding. **This is what `false_refusal_rate = 1.0000` and 57/75
`refusals_no_grounded_claims` in the f7/f8 deltas are recording** — those labels were measured on a
corpus-less DB, not a model or prompt failure.

```bash
cd backend
python -m app.ingestion.run --all                             # re-registers docs (dedupe_skip, no re-download)
python -m app.indexing.run --strategy fixed --namespace all   # rewrites chunks + upserts Pinecone (~$0.0003)
```

Ingestion must run **first** — `indexing.source.indexed_targets` reads its targets from the
`documents` table, so indexing alone reports "indexed 0 docs". Do not run pytest between the reseed
and the eval run.

## F9 — Semantic cache (Redis + Postgres) · gate: `f9-cache-after` vs `f8-compression-after`

Delta report: [`f9-cache-after-vs-f8-compression-after.md`](f9-cache-after-vs-f8-compression-after.md)
(**latency/cost suites only**, per the CLAUDE.md label-sequence note; same 588-chunk index). Two-tier
cache: Redis exact-match on `sha256(normalized_query)` checked *before* embedding, then a Postgres
`cache_entries` semantic tier searched by an in-process cosine matmul. Reproduce:

```bash
cd backend && set -a && . ./.env && set +a
# reseed first (see above), then:
export EVAL_LATENCY_REQUESTS=30 EVAL_LATENCY_UNIQUE_QUESTIONS=15   # a DECLARED 50% repeat rate
python -m app.caching.run --flush
python -m app.evals.run --suite latency \
    --flags hybrid=on,rerank=on,query_rewrite=on,compression=on,cache=off,memory=off \
    --label f8-compression-after --yes          # re-seeds the baseline at the matched workload
python -m app.caching.run --flush
python -m app.evals.run --suite latency \
    --flags hybrid=on,rerank=on,query_rewrite=on,compression=on,cache=on,memory=off \
    --label f9-cache-after --yes
python -m app.evals.run --label f9-cache-after --compare f8-compression-after
```

**Result (overall):** `cache_hit_rate` **0 → 0.500 ▲** — the workload's structural maximum (15
entries, 15 hits, one per entry; all 15 unique questions produced cacheable cited answers). **p50
latency 40.4 s → 3.9 s (−36.6 s, 10.5× ▲)** — at a 50% hit rate the median request *is* a hit. p95
−3.2 s and p99 −98.8 s (both still sit on the miss path). **The miss path got faster, not slower**
(`latency_searching_p50` −3.7 s ▲) — the gate's designated failure signal, since a miss-path
regression would mean the `query_vec` reuse was broken and the pipeline double-embedding. `$ saved`
**4.241e-05/request** (the table's 0.0000 is %.4f display precision, exactly as `cost_mean` has
always rendered).

**The `< 300 ms` AC is MISSED at 3,859 ms, structurally.** The cache key is F7's normalized question,
so with rewrite on a ~3.5 s `gpt-4o-mini` call precedes every lookup. Only the Redis exact tier can
clear 300 ms, and it was unmeasurable here (no Docker/Redis on the dev box) — the AC stands
**unverified**, not passed. Verified live instead: an exact repeat hits, replays byte-identically,
**18×** faster (69.2 s → 3.8 s).

**Ships default-off** (`ENABLE_CACHE=false`), A/B-gated. The semantic accept rule specced as
`cosine ≥ 0.95 AND Jaccard ≥ 0.3` was **measurably wrong** — against real `text-embedding-3-small`
vectors nothing reaches 0.95, and the two sets *overlap* (worst adversarial pair `15(3)` vs `15(4)` =
**0.930** > best true paraphrase = **0.912**), so no cosine threshold separates them and the Jaccard
floor is inert (optimal 0.0). What ships is `cosine ≥ 0.86 AND discriminative-token agreement`:
0 collisions on the committed set, but only **2/8** paraphrases at a **0.032** margin. Also measured:
**code-switched queries never hit** (0.33–0.47 cosine against their English twins) unless F7's
rewrite is on to normalize them first. Evidence: `backend/tests/cache/test_adversarial.py` (offline,
vectors committed).

> `tokens_mean` **33.0 → 17.0** and `cost_mean` in this delta are **artifacts, not wins** — a hit
> replays the answer as one `token` event and `tokens_mean` counts events. Both were deliberately
> left on their pre-F9 basis so the delta compares two pipelines, not two measurements.
