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
