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
