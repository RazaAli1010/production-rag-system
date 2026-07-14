# Eval results (F4)

Auto-generated reports from the F4 harness live here — one `{label}.md` per run and one
`{label}-vs-{prev}.md` per `--compare`. Each Phase B/C feature commits its delta report here as its
eval-gate artifact (fixed label sequence: `baseline → f5-hybrid-after → f6-rerank-after →
f7-rewrite-after → f8-compression-after → f9-cache-after → f17-memory-after`).

## Dataset status — SEED

`backend/app/data/evals/qa_dataset.jsonl` currently ships a **~17-record seed** covering every tag
(`en`, `code_switched`, `out_of_corpus`, `multi_doc`, `table_lookup`), grounded in the three corpus
docs (`pu-academic-probation-2023`, `pu-examination-rules-2022`, `hec-plagiarism-policy-2021`). It is
**not yet gate-ready**: `python -m app.evals.run --lint` intentionally reports it as under the spec
quotas (60–80 records, ≥15 `code_switched`, ≥10 `out_of_corpus`) until it is scaled. That "FAIL" is
the lint feature working correctly, not a broken build — CI runs `pytest tests/evals` (which uses
quota-meeting fixtures), never `--lint` on the seed.

**Follow-up before this dataset gates anything:** author it to 60–80 manually-verified records
against the live PU/HEC corpus, then re-run `--lint` until it passes.

## Recording the `baseline` label (needs live OpenAI/Pinecone keys + an ingested index)

```bash
cd backend
python -m app.evals.run --suite all --label baseline --yes     # writes baseline.md + DB rows
python -m app.evals.run --label baseline --compare baseline    # sanity: all-zero delta
```

`--yes` confirms the RAGAS judge spend (a cost preview prints first). Every run stamps the report
with the git SHA + index manifest so numbers are reproducible.
