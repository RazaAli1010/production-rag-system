# F5 — Hybrid Search (BM25 + Dense + RRF) · tasks.md

**Module:** `backend/app/rag/hybrid.py` (+ `retriever.py` body swap) · **Depends on:** F3, F4
**Flag:** `ENABLE_HYBRID` · **Eval gate:** `baseline` → `f5-hybrid-after`
Each task is scoped to ≈ ≤ 1 hour and carries its own test criterion. Ordering follows the data
flow: settings/contracts → BM25 load → sparse scoring → hydration → RRF → dispatcher → toggle wiring
→ refusal fix → degraded mode → acceptance → **eval gate**.

The final task **is** the mandatory Phase-B gate: run F4 `--suite all` with `hybrid=on`,
`--compare baseline`, and commit the delta report to `docs/eval_results/`.

---

### T1 — Settings + `AnswerResponse.degraded`
Add the F5 keys from `design.md §7` to `Settings` (`ENABLE_HYBRID`, `RETRIEVAL_MODE`,
`HYBRID_DENSE_TOP_K`, `HYBRID_SPARSE_TOP_K`, `HYBRID_FUSED_TOP_K`, `HYBRID_RRF_K`; reuse `BM25_PATH`).
Add `degraded: bool = False` to `AnswerResponse` in `core/contracts.py`.
**Test:** `Settings()` boots with defaults (`ENABLE_HYBRID is False`, `RETRIEVAL_MODE is None`) and
honours env overrides; `AnswerResponse(...)` defaults `degraded=False`; every existing F3/F4 test
that constructs an `AnswerResponse` still passes (additive-field proof).

### T2 — BM25 load (thread-offloaded, fail-fast, cached)
Implement `hybrid.load_bm25` (`anyio.to_thread.run_sync(pickle.load)`, module-level cache) and
`HybridIndexError`. Missing/unreadable `BM25_PATH` → `HybridIndexError` naming the path (AC-1/AC-2).
**Test:** loads a fixture pkl once and returns cached on the second call (patched loader called once,
AC-1); a non-existent path raises `HybridIndexError` with the path in the message (AC-2); the load
runs through `anyio.to_thread.run_sync` (spy on the offload, AC-19).

### T3 — Sparse scoring (Urdu-safe, inline)
Implement `hybrid.sparse_scores` reusing `app.indexing.bm25.urdu_safe_tokenize`; return top
`HYBRID_SPARSE_TOP_K` `(chunk_id, sparse_score, rank)` (1-indexed) from `BM25Okapi.get_scores`.
**Test:** on a synthetic `BM25Okapi` corpus, an exact-term query ranks the containing chunk #1; a
code-switched query preserves Urdu-range tokens (asserted against `urdu_safe_tokenize` directly,
AC-3/US-7); returns ≤ `HYBRID_SPARSE_TOP_K` triples, ranks contiguous from 1.

### T4 — Sparse-hit hydration via Pinecone `fetch`
Implement `hybrid.hydrate_sparse_only(ids, namespace, settings)`: async `index.fetch` for ids **not**
already in the dense set, building `RetrievedChunk` from F2 metadata (undo the `-1` page sentinel,
same shape as `retriever._none_if_sentinel`); `namespace=None` → fetch across
`settings.RETRIEVAL_NAMESPACES` and merge (`design.md §5`).
**Test (mocked index.fetch):** ids present in `pu` hydrate to full `RetrievedChunk`s; an id absent
from the fetched namespace is dropped (not raised, AC-4); `namespace=None` merges hits from both
namespaces; already-dense ids are **not** re-fetched (fetch called only with the sparse-only set).

### T5 — RRF fusion (custom, per-stage scores exposed)
Implement `hybrid.rrf_fuse(dense, sparse, sparse_chunks, settings)`: dedupe by `chunk_id` (AC-6);
`fused_score = Σ 1/(HYBRID_RRF_K + rank)` over the lists a chunk appears in (AC-7); populate
`dense_score`/`sparse_score`/`fused_score` (`None` where absent, AC-8); sort desc, cap at
`HYBRID_FUSED_TOP_K` (AC-9). Pure-CPU, inline.
**Test:** hand-computed `fused_score` ordering on synthetic ranked lists (AC-7); a chunk in **both**
lists yields one entry carrying both ranks and outranks equally-single-ranked chunks (AC-6);
sparse-only entry has `dense_score is None` and dense-only has `sparse_score is None` (AC-8); output
length ≤ 12 (AC-9). This is the `requirements §4.1`/`§4.2` acceptance test.

### T6 — `hybrid_retrieve` orchestration (parallel dense ∥ sparse)
Implement `hybrid.hybrid_retrieve`: `asyncio.gather(dense_retrieve(k=HYBRID_DENSE_TOP_K), sparse …)`
→ hydrate sparse-only → `rrf_fuse` → return the fused pool (≤12). Also `hybrid.sparse_only(...)` for
the `bm25_only` mode. No degraded handling yet (added in T9).
**Test (mocked dense + sparse):** returns fused `RetrievedChunk`s ordered by `fused_score`; dense and
sparse run concurrently (both awaited within one `gather`); the 12-pool is returned in full (seam
truncation to `k` is T7's job).

### T7 — Retriever dispatcher (seam body swap)
In `retriever.py`: rename F3's current `retrieve` body to `dense_retrieve` (helpers move unchanged);
add `resolve_mode(settings)` and a new `retrieve` dispatcher that routes `dense_only` →
`dense_retrieve`, `bm25_only` → `hybrid.sparse_only`, `hybrid` → `hybrid.hybrid_retrieve(...)[:k]`.
Signature unchanged (AC-16).
**Test:** with `ENABLE_HYBRID=false`/`RETRIEVAL_MODE=None`, `retrieve` returns exactly the
`dense_retrieve` result for a fixed mocked index (byte-for-byte baseline parity, AC-11/`§4.6`); with
`RETRIEVAL_MODE=hybrid` it returns `hybrid_retrieve[:k]`; `resolve_mode` precedence
(`RETRIEVAL_MODE` overrides `ENABLE_HYBRID`) asserted (AC-13).

### T8 — Toggle overlay wiring (`flags.apply_flags`, two call sites)
Implement `rag/flags.apply_flags(settings, flags)` (`model_copy(update={"ENABLE_HYBRID":
flags.hybrid})`). Wire it in `baseline._pipeline_events` (before retrieval) and in
`evals/retrieval.run_retrieval` (before the direct `retrieve` call) per `design.md §9`.
**Test:** `apply_flags(settings, PipelineFlags(hybrid=True)).ENABLE_HYBRID is True` and the original
`settings` is unmutated (copy, not in-place); `run_retrieval` with `flags.hybrid=True` drives the
hybrid path (spy asserts `retrieve` sees `ENABLE_HYBRID=True`); ragas/refusal/latency inherit the
toggle through `answer()` (AC-12). No change to any suite's scoring logic.

### T9 — Degraded mode (BM25-only fallback)
Add the degraded path to `hybrid_retrieve`: catch a dense failure while hybrid is on → BM25-only
hydrated results + `_DEGRADED.set(True)` + structlog `hybrid.degraded` (AC-14). Implement
`was_degraded()` (read+reset) and read it in `baseline._pipeline_events` onto
`AnswerResponse.degraded`.
**Test:** a mocked Pinecone dense failure yields BM25-only chunks, `degraded=True`, and **no** raise
(`requirements §4.3`); a healthy hybrid run leaves `degraded=False`; the ctxvar resets between runs
(no leakage across calls).

### T10 — Fusion-safe refusal gate
Change `refusal.pre_llm_gate` to refuse when `max(dense_score for c in chunks if c.dense_score is not
None)` (default `-inf` when none) `< REFUSAL_DENSE_THRESHOLD`, instead of `chunks[0].dense_score`
(AC-15). Keep the empty-chunks → refuse behaviour.
**Test:** a fused set whose top chunk is sparse-only (`dense_score None`) but with a supporting dense
chunk above threshold deeper in the pool does **not** refuse; a pool with every `dense_score` below
threshold **does** refuse; empty chunks still refuse (`requirements §4.4`). Existing F3 pre-gate
tests (dense-only, top chunk carries the score) still pass.

### T11 — Async grep-guard + no-migration confirmation
Assert `hybrid.py`/`flags.py` contain no sync twin (`\.invoke`, `embed_query`, blocking `requests`,
sync `redis`) — extend the `app/rag/` async grep-guard. Confirm `alembic revision --autogenerate`
produces an empty diff (no F5 schema change, `design.md §8`).
**Test:** the grep-guard test covers `hybrid.py`/`flags.py` and is green; `alembic check`/autogenerate
yields no new migration (AnswerResponse/RetrievedChunk are contracts, not tables).

### T12 — Acceptance / definition of done (unit + integration)
Wire the `requirements §4` acceptance suite end-to-end (mocked dense index + synthetic BM25 + mocked
`index.fetch`): RRF math + dedupe (T5), degraded mode (T9), refusal interaction (T10), Urdu
tokenizer parity (T3), and baseline toggle parity (T7). Add a hybrid smoke test: an exact-term /
section-number query that dense-only ranks poorly is surfaced into the top-5 by fusion.
**Definition of done:** `pytest tests/rag/` green including all `requirements §4.1–4.6` tests and the
async grep-guard; `ENABLE_HYBRID=false` proven identical to `baseline`; no Alembic migration added.

### T13 — EVAL GATE (mandatory Phase-B closer)
Run the F4 harness against the hybrid path and commit the delta report:

```bash
# ensure the dense index + bm25.pkl the baseline used are unchanged (same SHA/manifest);
# F5 adds no re-index (design §2) — dense vectors identical, so baseline numbers stay comparable.

python -m app.evals.run --suite all \
    --flags hybrid=on,rerank=off,query_rewrite=off,compression=off,memory=off \
    --label f5-hybrid-after --yes

python -m app.evals.run --label f5-hybrid-after --compare baseline
```

Then commit `docs/eval_results/f5-hybrid-after.md` and
`docs/eval_results/f5-hybrid-after-vs-baseline.md`.

Optional diagnostic A/Bs (not part of the fixed label sequence, for the report's analysis section):
`RETRIEVAL_MODE=bm25_only python -m app.evals.run --suite retrieval --label f5-bm25only` and a
`dense_only` run, to show the fusion win over each single retriever.

**Definition of done (the gate):** the delta table exists and is committed, mapping `f5-hybrid-after`
→ its git SHA + index manifest; hit@5 and MRR deltas vs `baseline` are reported **overall and per
slice**, with particular attention to `table_lookup` and `code_switched` (the slices hybrid is
expected to rescue). Per CLAUDE.md, **the feature is not done until this delta table is committed.**

---

**Gate label sequence (fixed):** `baseline` → **`f5-hybrid-after`** → `f6-rerank-after` → … F5's
"before" is the `baseline` report F4 produced; F6's "before" will be `f5-hybrid-after`. Every README
benchmark row for hybrid maps to the `f5-hybrid-after` label, which maps to a git SHA + index
manifest, so all numbers are reproducible.
