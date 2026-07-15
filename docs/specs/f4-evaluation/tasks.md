# F4 — Evaluation Harness (RAGAS + retrieval metrics) · tasks.md

**Module:** `backend/app/evals/` · **Depends on:** F3 · **Gates:** every Phase B/C feature
Each task is scoped to ≈ ≤ 1 hour and carries its own test criterion.

F4 is Phase A (the *tool* that implements the eval gate), so — like F3 — it carries **no `--compare`
gate against a predecessor**. Instead its final task *is* the gate machinery proven end-to-end:
produce the first real `baseline` report over F3 and show `--compare` renders a delta. From F5 onward
every enhancement's tasks.md ends by running this harness (`--label <f?-...-after>`, then
`--compare <prev label>` per the fixed CLAUDE.md label sequence) and committing the delta report.

Ordering follows the data flow: settings/schemas → dataset+lint → flags/manifest → the four suites →
harness+persistence → report → compare → dataset authoring → acceptance.

---

### T1 — Settings + F4 schemas
Add the F4 keys from `design.md §10` to the central `Settings` class (dataset path, results dir,
judge model, hit@k, RAGAS metrics + judge multiplier, latency requests/endpoint, concurrency,
dataset range + quotas); create `evals/schemas.py` with `EvalRecord`, `MetricValue`, `SuiteResult`,
`EvalRunReport`, `EvalConfig`.
**Test:** `Settings()` loads all new keys with defaults + env overrides; `EvalRecord` validates a
sample record and exposes `is_out_of_corpus`; `EvalConfig` round-trips via pydantic.
`pytest tests/evals/test_settings_schemas.py` green.

### T2 — Dataset loader + lint
Implement `dataset.load_dataset` (aiofiles JSONL → `list[EvalRecord]`, validate each line) and
`dataset.lint_dataset` (60–80 range, `code_switched ≥ 15`, `out_of_corpus ≥ 10`, no duplicate `qid`,
no empty `tags`) returning a list of failure reasons.
**Test:** a valid fixture dataset lints to `[]`; a dataset with 14 `code_switched` / a dup qid / an
empty-tags record returns the specific named reasons; a malformed JSON line aborts with the line/qid.

### T3 — Flag parsing + run manifest
Implement `flags.parse_flags` (`"hybrid=on,rerank=off"` → `PipelineFlags`; unknown key → `ValueError`;
**cache forced `False`** regardless of input, AC-20/27) and `manifest.git_sha` (async
`create_subprocess_exec` `git rev-parse HEAD`, `"unknown"` on failure) + `manifest.index_manifest_snapshot`
(reuse `app.indexing.manifest.read_manifest`).
**Test:** `parse_flags("hybrid=on,cache=on")` yields `hybrid=True, cache=False`; unknown key raises;
`git_sha()` returns a 40-hex string in-repo (or `"unknown"` when git call is mocked to fail);
snapshot returns `{}` when no manifest file exists.

### T4 — Retrieval suite (hit@k + MRR)
Implement `retrieval._is_hit` (doc_id AND page/anchor overlap, `design.md §5`), `_hit_at_k`,
`_reciprocal_rank`, and `run_retrieval` over the injected `retrieve` seam, per-slice + overall,
excluding `out_of_corpus`.
**Test:** `_is_hit` true on doc_id+page overlap, false on doc_id-only and on page-miss; a mocked
`retrieve` spy is asserted called (AC-5); hit@1<hit@3<hit@5 on a crafted ranking; `out_of_corpus`
records absent from every slice; MRR = 1/rank of first hit.

### T5 — RAGAS cost preview
Implement `ragas_suite.preview_judge_cost`: sum tiktoken tokens of
`question+answer+contexts+ground_truth` per record × `EVAL_RAGAS_JUDGE_MULTIPLIER`, convert via
`estimate_cost(EVAL_JUDGE_MODEL, ...)`, return `(tokens, usd)`.
**Test:** returns a strictly positive `(tokens, usd)` for a fixture set; uses the central
`estimate_cost` (asserted via spy), not a hand-rolled rate.

### T6 — RAGAS suite (thread-offloaded, confirm-gated)
Implement `run_ragas`: gather `{question, answer, contexts, ground_truth}` per answerable record via
the injected `answer`/`retrieve` seams (bounded `Semaphore`), print the T5 preview, and — only when
`confirm=True` — run RAGAS `evaluate()` via `anyio.to_thread.run_sync` with a `gpt-4o-mini` judge;
emit the four metrics as `MetricValue`s. Log tokens + `estimate_cost()` for the judge sweep.
**Test:** `confirm=False` returns an empty `SuiteResult` and makes **no** judge call (mock
call-count 0, AC-11); `confirm=True` invokes `evaluate` through `anyio.to_thread.run_sync` (patched
spy asserts the offload path, AC-12); `out_of_corpus` records are excluded.

### T7 — Refusal suite
Implement `run_refusal`: over the injected `answer` seam, compute refusal recall on `out_of_corpus`
and false-refusal rate on answerable records, plus a per-`refusal_reason` breakdown.
**Test (mocked `answer()`):** all out_of_corpus refused → recall `1.0`; one answerable wrongly
refused → false-refusal `1/answerable`; `low_retrieval_confidence` vs `no_grounded_claims`
breakdown reported (AC-15).

### T8 — Latency suite
Implement `run_latency`: N (`EVAL_LATENCY_REQUESTS`) `astream()` runs, timing total wall-clock and
per-stage from `StageEvent.ms`; mean output tokens + mean USD via `estimate_cost`; `_percentiles`
(p50/p95/p99) computed inline. Add the `EVAL_LATENCY_ENDPOINT` httpx/SSE branch (F11 mode) but leave
it dormant by default (endpoint `None`).
**Test:** `_percentiles` returns correct p50/p95/p99 on a known list; per-stage timings are read from
mocked `astream()` `StageEvent.ms` (not a re-timed probe, AC-17); with `EVAL_LATENCY_ENDPOINT=None`
no `httpx` call is made.

### T9 — Harness orchestration + persistence
Implement `harness.run_suites` (expand `--suite`, bounded fan-out, force `cache=False`/
`session_id=None`) and `harness._persist` (one `eval_runs` row with `git_sha` + index manifest +
flags; one `eval_results` row per `(metric, slice_tag)`), all async SQLAlchemy. Confirm **no** new
Alembic migration is needed (tables exist since `0001_initial`).
**Test:** `run_suites` over mocked suites writes exactly one `eval_runs` + N `eval_results` rows
(count assertion against an async-SQLite/session fixture, AC-21); `pipeline_flags`/`index_manifest`
persisted as JSONB dicts; `session_id=None` and `cache=False` on every seam call (spy assertion).

### T10 — Report writer
Implement `report.write_report`: render `docs/eval_results/{label}.md` (via `aiofiles`) — a header
with label/git_sha/index-manifest/flags and one table per suite (metric × slice), matching the
`docs/eval_results/` convention.
**Test:** for a fixture `EvalRunReport`, the file is written under `EVAL_RESULTS_DIR`, contains the
git SHA, every metric row, and per-slice sub-rows; re-running overwrites deterministically.

### T11 — CLI wiring (`run.py`)
Implement `run.py`: argparse (`--suite`, `--flags`, `--label`, `--compare`, `--lint`, `--yes`) +
`asyncio.run`; `--lint` runs T2 and exits with the reason list's status; a normal run calls
`run_suites` then `write_report`; `--compare` dispatches to T12.
**Test:** `--lint` exits non-zero on the broken fixture dataset (AC-3); `--suite retrieval --label x`
runs only the retrieval suite (AC-29); an unknown `--flags` key exits non-zero naming the key (AC-20).

### T12 — Comparison / delta report
Implement `compare.compare_labels`: load each label's most recent `eval_runs` + `eval_results`, join
on `(metric, slice_tag)`, emit the metric-aware delta table (latency/cost arrow flipped), print it,
and write `docs/eval_results/{current}-vs-{prev}.md`. Missing label → non-zero exit naming it.
**Test:** two seeded runs render a delta with correct signs and `▲/▼/=` direction (latency lower =
`▲`); a missing label exits non-zero naming it (AC-25); `baseline` vs itself yields an all-zero /
`=` table.

### T13 — Author + commit the QA dataset
Author `app/data/evals/qa_dataset.jsonl` (60–80 records: LLM-drafted from the corpus → manually
verified → plus real student-group questions), satisfying every quota (`code_switched ≥ 15`,
`out_of_corpus ≥ 10`, plus `en`/`multi_doc`/`table_lookup` coverage). Commit it to git.
**Test:** `python -m app.evals.run --lint` passes on the committed dataset (AC-2/3/4); the file is
tracked in git (versioned, not generated at runtime).

### T14 — Acceptance / definition of done
Wire end-to-end integration tests (mocked `retrieve`/`answer`/`astream` seams + RAGAS `evaluate`
patched) proving `requirements.md §4`:
1. `--suite all --label baseline --yes` produces `docs/eval_results/baseline.md` + `eval_runs`/
   `eval_results` rows with a captured git SHA + index manifest;
2. `--lint` passes on the real dataset and fails (non-zero, named quota) on the broken fixture;
3. the RAGAS cost preview is shown and the run spends nothing when confirm is declined;
4. `--compare baseline` renders + writes a delta table (proven `baseline` vs a second label, and
   `baseline` vs itself → all-zero);
5. the retrieval suite is asserted to call `retriever.retrieve` and the RAGAS/refusal suites to call
   `baseline.answer` (spy assertions) — proving F4 measures through the F3 seams, so F5+ need no F4
   change.
**Definition of done:** `pytest tests/evals/` green including all five acceptance tests and the
`app/evals/` async grep-guard (AC-30); confirmed (per `design.md §9`) that F4 adds **no** Alembic
migration since `eval_runs`/`eval_results` already exist (F12-owned).

---

**No `--compare` gate of its own:** F4 is the harness that *implements* the eval gate, not a feature
gated against a prior label — so, like F3, it ends at "eval-ready", not at a delta report against a
predecessor. The mandatory `--compare`/delta-report gate begins at **F5 (Hybrid)**, whose "before" is
the `baseline` label this feature produces. From F5 on, every Phase B/C tasks.md closes with: run
`python -m app.evals.run --suite <suites> --flags <...> --label <this-label>`, then
`--compare <previous-label>` (per the fixed sequence `baseline → f5-hybrid-after → f6-rerank-after →
f9-cache-after → f17-memory-after`), and commit the
resulting `docs/eval_results/{label}-vs-{prev}.md`.
