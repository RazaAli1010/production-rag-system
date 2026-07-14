# F4 — Evaluation Harness (RAGAS + retrieval metrics) · design.md

**Module:** `backend/app/evals/` · **Depends on:** F3 · **Gates:** every Phase B/C feature

---

## 1. Module layout

```
backend/app/evals/
├── __init__.py
├── run.py          # CLI: argparse + asyncio.run; --suite/--flags/--label/--compare/--lint/--yes
├── schemas.py      # EvalRecord, MetricValue, SuiteResult, EvalRunReport, EvalConfig (Pydantic)
├── dataset.py      # load_dataset(), lint_dataset(): JSONL -> list[EvalRecord] + quota enforcement
├── manifest.py     # git_sha() (async subprocess) + index-manifest snapshot (reuses F2 read_manifest)
├── flags.py        # parse_flags("hybrid=on,..."): str -> PipelineFlags, cache forced off (AC-20/27)
├── retrieval.py    # hit@1/3/5 + MRR over retriever.retrieve() (AC-5/6/7/8)
├── ragas_suite.py  # RAGAS 4 metrics via gpt-4o-mini judge, thread-offloaded (AC-9/10/12)
├── refusal.py      # refusal recall + false-refusal rate over baseline.answer() (AC-13/14/15)
├── latency.py      # p50/p95/p99 total + per-stage from StageEvent ms; tokens/$ per query (AC-16/17)
├── harness.py      # run_suites(): orchestrates suites, persists eval_runs/eval_results (AC-19/21)
├── report.py       # write_report(): docs/eval_results/{label}.md (AC-22)
└── compare.py      # compare_labels(): per-metric/per-slice delta table + {label}-vs-{prev}.md (AC-24)
```

Canonical models (`RetrievedChunk`, `Citation`, `AnswerResponse`, `PipelineFlags`, `StageEvent`) are
imported from `app.core.contracts`, never redefined. `app.indexing.cost.estimate_cost()` and
`app.indexing.manifest.read_manifest()` are reused verbatim (F2's central helpers). The two F3 seams —
`app.rag.retriever.retrieve` and `app.rag.baseline.answer` — are imported and called, never
re-implemented: this is the whole point of building F4 *after* F3.

---

## 2. What F4 measures, and through which seam

| Suite | Seam it drives | LLM? | Slices |
|---|---|---|---|
| retrieval | `retriever.retrieve(query, k, namespace, settings)` | none | overall + `en`/`code_switched`/`multi_doc`/`table_lookup` |
| ragas | `baseline.answer()` (answer) + `retriever.retrieve()` (contexts) | judge `gpt-4o-mini` | overall (answerable only) |
| refusal | `baseline.answer()` | primary LLM (per pipeline) | `out_of_corpus` (recall) vs answerable (false-refusal) |
| latency | `baseline.astream()` (per-stage `ms`) or HTTP `/api/ask` (F11, when present) | primary LLM | overall |

**The seam guarantee.** F5 replaces the *body* of `retriever.retrieve` (dense → dense+BM25 fusion) and
F6 inserts a rerank step, both preserving the `(query, k, namespace, settings) -> list[RetrievedChunk]`
signature. Because every suite calls that exact function (retrieval directly; ragas for its contexts;
refusal/latency transitively through `answer()`/`astream()`), re-measuring the enhanced pipeline is a
new `--label` run with **zero F4 edits** — the requirement's core promise.

### 2.1 Why RAGAS gets its contexts from a standalone `retrieve()`

`answer()` returns an `AnswerResponse` whose `citations` are only the *cited* chunks, not the full
retrieved context RAGAS needs for context_precision/recall. Rather than change the F3 `answer()`
signature (which F3's DoD forbids — "F4 needs zero F3 changes"), the RAGAS suite calls
`retriever.retrieve()` once for the contexts and `answer()` once for the answer. Retrieval is
deterministic for a fixed query + index + embedding model (`temperature=0`, same seam), so the
standalone contexts equal the ones `answer()` used internally. This is a deliberate tradeoff — one
extra cheap dense query per RAGAS record to avoid a cross-feature signature change — and is documented
here so a reviewer doesn't read it as an accidental double-retrieve.

---

## 3. Data-flow diagram

```
  python -m app.evals.run --suite all --flags hybrid=on,... --label f5-hybrid-after
        │
        ▼
  run.py: parse args ─► flags.parse_flags(...) ─► PipelineFlags (cache FORCED off, AC-27)
        │              └─► EvalConfig{label, flags, suites, confirm, compare_to}
        ▼
  dataset.load_dataset(settings)  ──►  list[EvalRecord]   (lint invariants re-checked, AC-1/2/4)
        │
        ▼
  harness.run_suites(cfg, settings, sessionmaker)
        │  bounded asyncio.gather (Semaphore = EVAL_CONCURRENCY) over records per suite
        │
        ├── retrieval ─► for each answerable record: retrieve(q, k, ns=None) ─► hit@k/MRR
        │                                    (out_of_corpus excluded, AC-7)     per slice
        │
        ├── ragas     ─► cost preview + confirm gate (AC-11) ─► for each answerable record:
        │                  contexts = retrieve(q,...) ; ans = answer(q, flags, session=None)
        │                  ─► RAGAS evaluate(...) via anyio.to_thread.run_sync (AC-12)
        │
        ├── refusal   ─► for each record: ans = answer(q, flags)
        │                  out_of_corpus  ─► refused? (recall)     answerable ─► refused? (false-refusal)
        │
        └── latency   ─► N requests: astream(q) timing total + per-stage StageEvent.ms (AC-16/17)
        │
        ▼
  MetricValue rows  ─►  persist: eval_runs (git_sha + index_manifest + flags) + eval_results (AC-21)
        │                └─ report.write_report(...) ─► docs/eval_results/{label}.md  (AC-22)
        ▼
  (separate invocation) run.py --label f5-hybrid-after --compare f6-rerank-after... no —
  --compare baseline ─► compare.compare_labels(current, prev) ─► delta table + {label}-vs-{prev}.md
```

**Which side of the async line each node falls on** (CLAUDE.md mandate):
- I/O awaited on the loop: `retrieve()` (Pinecone/embeddings), `answer()`/`astream()` (LLM),
  async SQLAlchemy writes, `aiofiles` report writes, `git rev-parse` via `asyncio.create_subprocess_exec`.
- **Off the loop via `anyio.to_thread.run_sync`:** RAGAS's synchronous `evaluate()` — a blocking
  CPU+IO judge sweep; running it inline would stall the loop and trip the async grep-guard's intent.
- Cheap pure-CPU inline: percentile math (`statistics`/`numpy` on the latency list), hit@k set
  overlap, tiktoken counting for the cost preview.

---

## 4. Key schemas & function signatures

```python
# schemas.py
class EvalRecord(BaseModel):
    qid: str
    question: str
    ground_truth_answer: str
    source_doc_ids: list[str]
    source_pages_or_anchors: list[str]      # ints coerced to str; anchors as-is
    tags: list[str]                          # >=1 (AC-4)

    @property
    def is_out_of_corpus(self) -> bool: return "out_of_corpus" in self.tags

class MetricValue(BaseModel):
    metric: str                              # "hit@5", "mrr", "faithfulness", "latency_p95", "cost_mean"
    value: float
    slice_tag: str | None = None             # None = overall; else the tag slice (AC-7)

class SuiteResult(BaseModel):
    suite: str                               # retrieval|ragas|refusal|latency
    metrics: list[MetricValue]

class EvalRunReport(BaseModel):
    label: str
    git_sha: str
    index_manifest: dict
    pipeline_flags: dict
    suites: list[SuiteResult]
    report_path: str | None = None

class EvalConfig(BaseModel):
    label: str
    flags: PipelineFlags                     # cache always False (flags.parse_flags forces it)
    suites: list[str]                        # expanded from --suite ("all" -> the four)
    confirm: bool = False                    # --yes
    compare_to: str | None = None

# dataset.py
async def load_dataset(settings) -> list[EvalRecord]: ...          # aiofiles JSONL read + validate
def lint_dataset(records: list[EvalRecord], settings) -> list[str]: ...  # [] = pass; else reasons (AC-3/4)

# flags.py
def parse_flags(spec: str | None) -> PipelineFlags: ...            # k=on/off; unknown key -> ValueError; cache=False

# manifest.py
async def git_sha() -> str: ...                                    # `git rev-parse HEAD`, async subprocess
async def index_manifest_snapshot(settings) -> dict: ...          # read_manifest(settings).model_dump() or {}

# retrieval.py
async def run_retrieval(records, flags, settings, *, retrieve=retriever_mod.retrieve) -> SuiteResult: ...
def _is_hit(chunk: RetrievedChunk, rec: EvalRecord) -> bool: ...   # doc_id AND page/anchor overlap (AC-6)
def _hit_at_k(ranked: list[RetrievedChunk], rec: EvalRecord, k: int) -> float: ...
def _reciprocal_rank(ranked: list[RetrievedChunk], rec: EvalRecord) -> float: ...

# ragas_suite.py
async def run_ragas(records, flags, settings, *, confirm: bool,
                    answer=baseline.answer, retrieve=retriever_mod.retrieve,
                    sessionmaker=...) -> SuiteResult: ...
def preview_judge_cost(records, settings) -> tuple[int, float]: ...   # (tokens, usd) via estimate_cost (AC-11)

# refusal.py
async def run_refusal(records, flags, settings, *, answer=baseline.answer,
                      sessionmaker=...) -> SuiteResult: ...

# latency.py
async def run_latency(records, flags, settings, *, astream=baseline.astream,
                      sessionmaker=...) -> SuiteResult: ...
def _percentiles(samples: list[float]) -> dict[str, float]: ...      # p50/p95/p99, inline CPU

# harness.py
async def run_suites(cfg: EvalConfig, *, settings, sessionmaker) -> EvalRunReport: ...
async def _persist(report: EvalRunReport, *, sessionmaker) -> None: ...   # eval_runs + eval_results (AC-21)

# report.py
async def write_report(report: EvalRunReport, settings) -> str: ...      # docs/eval_results/{label}.md

# compare.py
async def compare_labels(current: str, prev: str, *, settings, sessionmaker) -> str: ...  # delta md path
```

The suites take their seam functions as **default keyword arguments** (`retrieve=...`, `answer=...`,
`astream=...`) so tests inject spies/mocks without patching imports — and so AC-5 ("scored via
`retriever.retrieve`, asserted via a spy") is a one-line test.

---

## 5. Hit / MRR definition (AC-6, worked)

```python
def _is_hit(chunk, rec):
    if chunk.doc_id not in rec.source_doc_ids:
        return False
    labels = set(rec.source_pages_or_anchors)          # e.g. {"12", "13", "clause-7.2"}
    if chunk.anchor and chunk.anchor in labels:
        return True
    if chunk.page_start is not None:
        pages = range(chunk.page_start, (chunk.page_end or chunk.page_start) + 1)
        return any(str(p) in labels for p in pages)     # page overlap
    return False
```

- `hit@k = 1.0 if any(_is_hit(c, rec) for c in ranked[:k]) else 0.0`, averaged over records in the
  slice.
- `MRR = mean(1/rank_of_first_hit)`, `0.0` when no hit in the returned list.
- `out_of_corpus` records are excluded from the retrieval suite entirely (they have no labeled
  source) — they belong to the refusal suite (AC-7/13).

---

## 6. RAGAS integration & the cost gate (AC-9/10/11/12)

RAGAS is fed a dataset of `{question, answer, contexts, ground_truth}` and scored with a
`gpt-4o-mini` judge (`ChatOpenAI` wrapped as RAGAS's `LangchainLLMWrapper`) + `OpenAIEmbeddings` for
answer_relevancy. Metrics: `faithfulness`, `answer_relevancy`, `context_precision`, `context_recall`.

**Cost gate (AC-11).** `preview_judge_cost()` estimates judge tokens ≈
`Σ_records (question + answer + joined_contexts + ground_truth tokens) × RAGAS_JUDGE_MULTIPLIER`
(the multiplier accounts for RAGAS issuing several judge prompts per record), converts to USD via
`estimate_cost(settings.EVAL_JUDGE_MODEL, tokens_in, tokens_out_est)`, prints both, and returns. The
suite proceeds only if `confirm` is `True` (`--yes`) — otherwise it logs the estimate and returns an
empty `SuiteResult`, spending nothing.

**Off the loop (AC-12).** RAGAS's `evaluate()` is synchronous and blocking. It is invoked via
`await anyio.to_thread.run_sync(functools.partial(evaluate, ...))`, so the judge sweep never occupies
the event loop — the same "CPU/IO-bound work off the loop" rule F3 applied to nothing and F2 applied
to OCR/BM25. Answers and contexts for all records are gathered *first* (bounded async fan-out over the
F3 seams), then handed to the single thread-offloaded `evaluate()` call.

---

## 7. Latency suite (AC-16/17/18)

Default mode drives `baseline.astream()` in-process for `EVAL_LATENCY_REQUESTS` requests (sampled with
replacement from answerable records), timing:
- **total:** wall-clock around the full `astream()` consumption per request.
- **per-stage:** the `ms` already carried on the `searching`/`generating`/`citing` `StageEvent`s (F3
  emits paired started/done with `ms` on done) — so latency attribution reuses F3's existing
  instrumentation and adds no blocking probe (AC-17).
- **tokens/$:** mean output tokens (counted from streamed `token` events) and mean USD via
  `estimate_cost()` per query.

`_percentiles()` computes p50/p95/p99 inline (cheap pure-CPU). When `settings.EVAL_LATENCY_ENDPOINT`
is set (F11's `/api/ask` exists), the suite instead drives that URL with `httpx.AsyncClient` consuming
the SSE stream — same metrics, real network path. Per AC-18 this suite is gated only at
`f9-cache-after` / `f17-memory-after`; earlier it is informational.

---

## 8. Comparison — the eval-gate artifact (AC-24/25/26)

`compare_labels(current, prev)` loads the most recent `eval_runs` row for each label + its
`eval_results`, joins on `(metric, slice_tag)`, and emits a markdown table:

```
| metric        | slice         | prev (baseline) | current (f5-hybrid-after) | Δ      | dir |
|---------------|---------------|-----------------|---------------------------|--------|-----|
| hit@5         | overall       | 0.72            | 0.81                      | +0.09  | ▲   |
| hit@5         | code_switched | 0.55            | 0.68                      | +0.13  | ▲   |
| faithfulness  | overall       | 0.88            | 0.90                      | +0.02  | ▲   |
| mrr           | table_lookup  | 0.61            | 0.58                      | -0.03  | ▼   |
```

Direction (`▲`/`▼`/`=`) is metric-aware: for latency/cost metrics *lower is better*, so the arrow is
flipped (a `_HIGHER_IS_BETTER` set drives the sign). Missing label → non-zero exit naming it (AC-25).
Output is printed **and** written to `docs/eval_results/{current}-vs-{prev}.md` — the file each Phase B
feature commits as its gate artifact.

---

## 9. Alembic migrations

**None.** `eval_runs` and `eval_results` already exist (created in `0001_initial`, owned by F12 and
referenced by name in the CLAUDE.md data contract). Their columns cover everything F4 records —
`eval_runs(label, git_sha, index_manifest JSONB, pipeline_flags JSONB, started_at)` and
`eval_results(run_id, metric, value, slice_tag)`. Every F4 metric (hit@k, mrr, faithfulness,
answer_relevancy, context_precision, context_recall, refusal_recall, false_refusal_rate,
latency_p50/p95/p99, latency_{stage}_p95, tokens_mean, cost_mean) serializes to a `(metric, value,
slice_tag)` row — no new column, so **no migration**. Called out explicitly per the same
no-migration-note convention F2/F3 used.

---

## 10. New Settings keys (central `app.core.settings.Settings`)

```python
# --- Evaluation harness (F4) ---
EVAL_DATASET_PATH: Path = Path("app/data/evals/qa_dataset.jsonl")   # git-versioned QA set (AC-1)
EVAL_RESULTS_DIR: Path = Path("../docs/eval_results")               # repo-root docs (same rel-cwd trick as INGESTION_REPORT_DIR)
EVAL_JUDGE_MODEL: str = "gpt-4o-mini"                               # RAGAS judge (AC-9)
EVAL_HIT_KS: list[int] = [1, 3, 5]                                  # hit@k cutoffs (AC-5)
EVAL_RETRIEVAL_K: int = 5                                           # k passed to retrieve() (>= max EVAL_HIT_KS)
EVAL_RAGAS_METRICS: list[str] = [
    "faithfulness", "answer_relevancy", "context_precision", "context_recall",
]
EVAL_RAGAS_JUDGE_MULTIPLIER: float = 4.0                            # judge prompts per record, for cost preview (AC-11)
EVAL_LATENCY_REQUESTS: int = 100                                    # AC-16
EVAL_LATENCY_ENDPOINT: str | None = None                           # F11 /api/ask URL; None = in-process astream (AC-17)
EVAL_CONCURRENCY: int = 4                                           # bounded async fan-out over records (Semaphore)
EVAL_DATASET_MIN: int = 60
EVAL_DATASET_MAX: int = 80                                          # 60-80 range (AC-2)
EVAL_QUOTA_CODE_SWITCHED: int = 15                                 # AC-3
EVAL_QUOTA_OUT_OF_CORPUS: int = 10                                 # AC-3
```

`LLM_MODEL`, `RETRIEVAL_NAMESPACES`, `EMBED_MODEL`, `PINECONE_*`, `INDEX_MANIFEST_PATH` are reused
from F2/F3 verbatim (the harness must retrieve/generate with the same models and index the pipeline
uses). No prior key is redefined.

---

## 11. Error handling

| Failure | Detection | Handling |
|---|---|---|
| Dataset file missing / bad JSON | `aiofiles` read + `EvalRecord` validation | abort with the path + the offending `qid`/line (AC-1) |
| Quota / range / duplicate-qid violation | `lint_dataset()` invariants | non-zero exit listing every failing quota (AC-3/4) |
| Unknown `--flags` key | `parse_flags` membership check vs `PipelineFlags` fields | `ValueError` → non-zero exit naming the key (AC-20) |
| RAGAS run without confirm | `confirm is False` after preview | print estimate, return empty `SuiteResult`, spend nothing (AC-11) |
| LLM/judge 429 or 5xx | reuses F3 `errors.is_retryable` via `answer()`/`retrieve()` retry paths | inherited from F3; a record that still fails is recorded as a per-record error and excluded from that metric's mean (logged), so one bad record can't void a whole suite |
| `--compare` label absent | no `eval_runs` row for label | non-zero exit naming the missing label (AC-25) |
| Latency endpoint unreachable (F11 mode) | `httpx` connect error | abort with the endpoint URL; suggest omitting `EVAL_LATENCY_ENDPOINT` for in-process mode |
| Git SHA unavailable (detached/no git) | subprocess non-zero | record `git_sha="unknown"` + `structlog` warning rather than abort the run |

---

## 12. Honoring the Shared Context contracts & the F3 seam

- **F3 retriever seam:** the retrieval suite calls `retriever.retrieve` directly; RAGAS pulls its
  contexts through it; refusal/latency reach it transitively via `answer()`/`astream()`. F5/F6 swap
  the seam body → F4 re-measures with zero edits (design §2). This is the single most important
  contract F4 honors.
- **`AnswerResponse`:** refusal suite reads `refused`/`refusal_reason`; RAGAS reads `answer`; latency
  reconstructs answer text from `token` events. No field is added or reinterpreted.
- **`RetrievedChunk`:** hit scoring reads `doc_id`/`page_start`/`page_end`/`anchor`; `dense_score`
  order defines rank. F5's added `fused_score`/`rerank_score` don't change hit logic (rank order is
  whatever the seam returns) — so the metric definition survives every Phase B seam change.
- **`StageEvent`:** latency per-stage timing reuses the `ms` F3 already emits on
  `searching`/`generating`/`citing` done events — no new stage, no blocking probe (AC-17).
- **`PipelineFlags`:** `--flags` parses into it; cache is forced off and `session_id=None`
  (AC-27) so labels stay comparable — the CLAUDE.md "harness always runs skip_cache=true and
  session_id=None" rule, enforced in `flags.parse_flags` + every suite's `answer()` call.
- **`eval_runs`/`eval_results` (F12-owned):** written by name via async SQLAlchemy; the git SHA +
  index manifest on each run make every README benchmark row reproducible (the eval-gate label →
  SHA → manifest chain from CLAUDE.md).
- **Cost rule:** every judge/generation call logs tokens + `estimate_cost()` (reused); the RAGAS
  preview uses the same helper so the estimate and the actuals share one cost model.
- **Async rule:** no sync twin in `app/evals/`; RAGAS's blocking `evaluate()` is the one CPU/IO
  offload, run via `anyio.to_thread.run_sync` (stated explicitly per the "which side of the line"
  rule).

---

## 13. Test strategy (see tasks.md for the ordered list)

- **Fixtures:** a small `EvalRecord` set (a few answerable across `en`/`code_switched`/`multi_doc`/
  `table_lookup` + ≥1 `out_of_corpus`); a deliberately-broken dataset for the lint-failure test; the
  F3 `retrieve`/`answer` seams injected as spies/mocks via the suites' default-kwarg seam params.
- **Unit:** `_is_hit` doc_id+page/anchor overlap (and non-overlap); `_hit_at_k`/`_reciprocal_rank`
  ranking math; `parse_flags` on/off + unknown-key `ValueError` + cache-forced-off; `lint_dataset`
  each quota; `_percentiles` p50/p95/p99; `preview_judge_cost` returns a positive estimate;
  metric-aware direction in `compare` (latency arrow flipped).
- **Integration (mocked seams):** `run_retrieval` asserts it called the injected `retrieve` spy
  (AC-5) and excludes `out_of_corpus`; `run_ragas` aborts without spending when `confirm=False`
  (AC-11) and thread-offloads `evaluate` when `confirm=True` (AC-12); `run_refusal` computes recall +
  false-refusal from mocked `answer()` refusals; `harness.run_suites` persists one `eval_runs` +
  N `eval_results` rows (AC-21) and writes `{label}.md`; `compare_labels` renders a delta and
  non-zero-exits on a missing label (AC-24/25).
- **Grep guard:** assert no `.invoke`/`embed_documents`/`requests`/sync-`redis` token appears in
  `app/evals/` (AC-30), matching F2/F3's async grep-guard test.
