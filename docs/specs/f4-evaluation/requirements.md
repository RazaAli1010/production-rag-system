# F4 — Evaluation Harness (RAGAS + retrieval metrics) · requirements.md

**Module:** `backend/app/evals/` · **Phase:** A (foundation) · **Depends on:** F3 (Baseline RAG)
**Gates:** every Phase B/C enhancement (F5–F9, F17)
**Status of eval gate:** F4 *is* the eval gate — it is the tool every later feature runs, not a
feature that is itself gated against a prior label. Its own DoD is producing the first real
`baseline` report over F3 and proving `--compare` renders a delta, so that from F5 onward recording
a label and diffing the previous one is a pure F4 CLI run with **zero enhancement-code changes**.

---

## 1. Overview

F4 is the measurement backbone. One command — `python -m app.evals.run` — evaluates any pipeline
configuration against a versioned QA dataset and writes results to Postgres (`eval_runs` /
`eval_results`, owned by F12) plus an auto-generated markdown report under `docs/eval_results/`.
Built immediately after the F3 baseline so every Phase B/C enhancement has a reproducible
before/after story.

The harness measures **through the F3 seams, never around them**: the retrieval suite drives
`app.rag.retriever.retrieve()` (the exact `(query, k, namespace, settings) -> list[RetrievedChunk]`
callable F5 swaps the body of), and the generation/refusal suites drive `app.rag.baseline.answer()`
(the deterministic non-streaming entry point F3 built for exactly this). Because it measures through
those seams, swapping dense-only for hybrid (F5) or inserting a reranker (F6) needs **no F4 change** —
the same harness re-measures the new pipeline under a new `--label`.

Five suites, one CLI:

1. **retrieval** — hit@1/3/5, MRR, per pipeline configuration and per tag slice.
2. **ragas** — faithfulness, answer_relevancy, context_precision, context_recall (`gpt-4o-mini`
   judge), with a cost preview + confirm gate before it runs.
3. **refusal** — refusal recall on out-of-corpus probes; false-refusal rate on answerable questions.
4. **latency** — p50/p95/p99 total + per-stage over 100 requests; mean tokens and USD per query.
5. **all** — runs the four above and writes one combined report.

Comparison (`--compare <prev-label>`) prints and writes a per-metric, per-slice delta table between
two labels. **That delta table is the eval-gate artifact each Phase B feature commits.**

F4 does **not** implement a human-eval UI, online A/B, or CI-scheduled runs — those are stretch
notes (see §5).

---

## 2. User stories

- **US-1 (Enhancement author, e.g. F5 hybrid):** As the author of a Phase B feature, I want one
  command that scores the pipeline *before* and *after* my change so I can prove the enhancement
  actually improved retrieval/generation and not just shifted numbers.
- **US-2 (Enhancement author):** As a Phase B/C author, I want the harness to measure through the
  same F3 `retrieve()` / `answer()` seams my feature swaps, so I never have to touch eval code to be
  measured — I only flip a flag.
- **US-3 (Reviewer / eval-gate approver):** As the person approving a merge, I want a committed
  `docs/eval_results/{label}.md` delta report tied to a git SHA + index manifest, so every README
  benchmark number is reproducible and traceable to an exact code + index state.
- **US-4 (Dataset maintainer):** As the person curating the QA set, I want a lint script that fails
  when tag quotas or the 60–80 record range are violated, so the dataset can't silently drift below
  the slices the gate depends on.
- **US-5 (Cost owner):** As the person paying the OpenAI bill, I want a judge-cost preview and an
  explicit confirm before any RAGAS run, so I never accidentally launch a large LLM-judge sweep.
- **US-6 (Ops / comparability owner):** As the person trusting cross-label deltas, I want the harness
  to always bypass the semantic cache and use no session memory, so retrieval and generation metrics
  stay comparable across every label regardless of runtime caching.
- **US-7 (Refusal-threshold tuner):** As the person tuning the F3/F6 refusal thresholds, I want a
  suite that reports both refusal recall (on out-of-corpus) and false-refusal rate (on answerable)
  so I can move the threshold with evidence, not guesswork.
- **US-8 (Latency owner):** As the person answering "is it fast enough on a student's phone", I want
  per-stage p50/p95/p99 and mean tokens/$ per query, so I can attribute latency and cost to the
  stage that owns it.

---

## 3. EARS acceptance criteria

### 3.1 Dataset & lint
- **AC-1 (Ubiquitous):** The system shall read its QA dataset from a git-versioned JSONL file at
  `settings.EVAL_DATASET_PATH` (`app/data/evals/qa_dataset.jsonl`), each record carrying
  `{qid, question, ground_truth_answer, source_doc_ids, source_pages_or_anchors, tags}`.
- **AC-2 (Ubiquitous):** The dataset shall contain 60–80 records with tags drawn from
  `{en, code_switched, out_of_corpus, multi_doc, table_lookup}`.
- **AC-3 (Unwanted — quota violation):** If the dataset holds fewer than 15 `code_switched`, fewer
  than 10 `out_of_corpus`, or a total outside 60–80, then the lint command shall exit non-zero and
  name the failing quota — it shall not silently pass.
- **AC-4 (Ubiquitous):** The system shall reject a dataset with duplicate `qid`s or a record whose
  `tags` are empty, so every record maps to at least one measurable slice.

### 3.2 Retrieval suite
- **AC-5 (Ubiquitous):** The retrieval suite shall compute hit@1, hit@3, hit@5, and MRR by driving
  `app.rag.retriever.retrieve()` (the F3→F5 seam) — never a re-implemented retrieval path.
- **AC-6 (Ubiquitous):** A retrieved chunk shall count as a hit when its `doc_id` is in the record's
  `source_doc_ids` **and** its page range (`page_start..page_end`) or `anchor` overlaps the record's
  `source_pages_or_anchors`; MRR shall use the rank of the first such hit.
- **AC-7 (Ubiquitous):** The retrieval suite shall report every metric both overall and per tag slice
  (`en`, `code_switched`, `multi_doc`, `table_lookup`); `out_of_corpus` records shall be excluded
  from retrieval scoring.
- **AC-8 (Ubiquitous):** The retrieval suite shall run without any LLM call, so it is the cheap suite
  a Phase B author can iterate on freely.

### 3.3 Generation suite (RAGAS)
- **AC-9 (Ubiquitous):** The RAGAS suite shall compute faithfulness, answer_relevancy,
  context_precision, and context_recall using `gpt-4o-mini` (`settings.EVAL_JUDGE_MODEL`) as the
  judge, over answerable records only (`out_of_corpus` probes excluded).
- **AC-10 (Ubiquitous):** The RAGAS suite shall obtain each record's answer via
  `app.rag.baseline.answer()` and its contexts via `app.rag.retriever.retrieve()`, so it measures the
  same seams every other suite measures.
- **AC-11 (Event-driven — cost preview):** Before issuing any judge call, the system shall print an
  estimated token count and USD cost (via the central `app.indexing.cost.estimate_cost()`), and shall
  proceed only when `--yes` is passed or the operator confirms — otherwise it shall abort without
  spending.
- **AC-12 (State-driven — RAGAS off the loop):** While RAGAS's synchronous `evaluate()` runs, the
  system shall execute it via `anyio.to_thread.run_sync` so the blocking judge sweep never runs on
  the event loop (honoring the CLAUDE.md async mandate for code under `app/`).

### 3.4 Refusal suite
- **AC-13 (Ubiquitous):** The refusal suite shall report refusal recall = fraction of `out_of_corpus`
  probes for which `answer()` returned `refused=True`.
- **AC-14 (Ubiquitous):** The refusal suite shall report false-refusal rate = fraction of answerable
  records (all non-`out_of_corpus`) for which `answer()` wrongly returned `refused=True`.
- **AC-15 (Ubiquitous):** The refusal suite shall record which `refusal_reason`
  (`low_retrieval_confidence` vs `no_grounded_claims`) drove each refusal, so the F3/F6 threshold
  tuner sees whether the pre-LLM gate or the zero-citation guard fired.

### 3.5 Latency / cost suite
- **AC-16 (Ubiquitous):** The latency suite shall issue `settings.EVAL_LATENCY_REQUESTS` (default 100)
  requests and report p50/p95/p99 of total wall-clock latency plus mean tokens and mean USD per query.
- **AC-17 (Ubiquitous):** The latency suite shall report per-stage p50/p95/p99 using the `ms` carried
  on the F3 `searching`/`generating`/`citing` `StageEvent`s consumed from `astream()`, attributing
  latency to the stage that owns it without adding any blocking instrumentation.
- **AC-18 (Ubiquitous):** The latency suite shall run only for the `f9-cache-after` and
  `f17-memory-after` labels per the fixed eval-gate label sequence; for earlier labels it may be
  invoked but is documented as informational, since caching/memory are the only features it measures.

### 3.6 Harness CLI, persistence & manifest
- **AC-19 (Ubiquitous):** The system shall expose
  `python -m app.evals.run --suite retrieval|ragas|refusal|latency|all --flags <k=v,...>
  --label <str>` as an `asyncio.run` entrypoint.
- **AC-20 (Ubiquitous):** The `--flags` string (`hybrid=on,rerank=off,...`) shall parse into a
  `PipelineFlags`; an unknown flag key shall abort with a clear error rather than being silently
  ignored.
- **AC-21 (Ubiquitous):** Each run shall write one `eval_runs` row (`label`, `git_sha`,
  `index_manifest` snapshot, `pipeline_flags`) and one `eval_results` row per (metric, slice_tag)
  pair, all via async SQLAlchemy — the same tables F12 created (no new migration).
- **AC-22 (Ubiquitous):** Each run shall auto-generate `docs/eval_results/{label}.md` summarizing
  every metric overall and per slice, stamped with the git SHA and index manifest.
- **AC-23 (Ubiquitous):** The system shall capture the git SHA (`git rev-parse HEAD` via async
  subprocess) and the current index manifest (`app.indexing.manifest.read_manifest`, reused) so every
  recorded number is reproducible.

### 3.7 Comparison / the eval-gate artifact
- **AC-24 (Ubiquitous):** `--compare <prev-label>` shall render a per-metric, per-slice delta table
  between the current label's run and `<prev-label>`'s most recent run, printed to stdout and written
  to `docs/eval_results/{label}-vs-{prev-label}.md`.
- **AC-25 (Unwanted — missing label):** If either compared label has no `eval_runs` row, then the
  system shall exit non-zero naming the missing label rather than emit a partial or empty table.
- **AC-26 (Ubiquitous):** The delta table shall mark each metric's direction (improved/regressed/flat)
  so a reviewer reads the gate outcome without recomputing signs.

### 3.8 Comparability, toggling & cross-cutting
- **AC-27 (Ubiquitous):** The harness shall always force cache bypass (`skip_cache=true`, cache flag
  off) and `session_id=None` regardless of the `--flags` string, so retrieval/generation metrics stay
  comparable across labels (CLAUDE.md comparability rule).
- **AC-28 (Ubiquitous):** Every OpenAI call the harness makes (RAGAS judge, any answer generation)
  shall log token usage and estimated USD via `app.indexing.cost.estimate_cost()` — reused, not
  reimplemented.
- **AC-29 (Ubiquitous):** The suite selection is itself the toggle: `--suite <name>` runs exactly the
  named suite (or `all`), so any suite can be included/excluded per invocation without code change.
- **AC-30 (Ubiquitous):** No sync twin (`invoke`, `embed_documents`, blocking `requests`, sync
  `redis`) shall appear anywhere in `app/evals/`; all pipeline, DB, HTTP, and file I/O shall use the
  async surface, and RAGAS's blocking `evaluate()` shall be thread-offloaded (AC-12).

---

## 4. Acceptance criteria (feature-level definition of done)

1. `python -m app.evals.run --suite all --label baseline --yes` produces the full
   `docs/eval_results/baseline.md` report (all four suites) with `eval_runs`/`eval_results` rows and a
   captured git SHA + index manifest.
2. `python -m app.evals.run --lint` passes on the committed dataset and fails (non-zero, named quota)
   when a quota is deliberately broken in a fixture dataset.
3. The RAGAS judge-cost preview is printed and the run aborts without spending when confirm is
   declined / `--yes` absent.
4. `--compare baseline` renders a delta table between two labels (proven with a second label, or
   `baseline` vs itself yielding an all-zero delta) and writes the `{label}-vs-{prev}.md` artifact.
5. The retrieval suite scores hit@k/MRR by calling `app.rag.retriever.retrieve()` (asserted via a spy)
   and the RAGAS/refusal suites by calling `app.rag.baseline.answer()` — proving F4 measures through
   the F3 seams, so F5+ need no F4 change.
6. Every AC above is asserted by an automated test — this list is the test list, not aspirational
   prose.

---

## 5. Out of scope (do not implement here)

- Human-eval / annotation UI, online A/B experimentation, and CI-scheduled nightly eval runs — the
  nightly GitHub Action is a stretch note only.
- Building or modifying the retrieval/generation pipeline itself — F4 only *measures* F3's seams; it
  never re-implements retrieval, prompting, or citation logic.
- New Postgres tables or columns — `eval_runs`/`eval_results` are owned by F12 and used as-is; F4
  adds **no** Alembic migration (design.md §9).
- Latency measurement of features that don't yet exist — the latency suite gates only at
  `f9-cache-after` / `f17-memory-after`; earlier invocations are informational (AC-18).
