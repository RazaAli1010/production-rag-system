# F6 — Cross-Encoder Reranking · tasks.md

**Module:** `backend/app/rag/rerank.py` (+ rerank step in `retriever.retrieve`) · **Depends on:**
F5, F4 · **Flag:** `ENABLE_RERANK` · **Eval gate:** `f5-hybrid-after` → `f6-rerank-after`
Each task is scoped to ≈ ≤ 1 hour and carries its own test criterion. Ordering follows the data
flow: settings → model singleton → score/calibrate → reorder → seam step → toggle wiring → gate swap
→ API surface → async/no-migration guards → acceptance → **eval gate**.

The final task **is** the mandatory Phase-B gate: run F4 `--suite all` with `hybrid=on,rerank=on`,
`--compare f5-hybrid-after`, and commit the delta report to `docs/eval_results/`.

---

### T1 — Settings (F6 keys)
Add the F6 keys from `design.md §7` to `Settings` (`ENABLE_RERANK`, `RERANK_MODEL`, `RERANK_DEVICE`,
`RERANK_TOP_N`, `RERANK_CANDIDATE_K`, `RERANK_APPLY_SIGMOID`, `REFUSAL_RERANK_THRESHOLD`; reuse
`HYBRID_FUSED_TOP_K`). `ENABLE_RERANK` joins the feature-flag block.
**Test:** `Settings()` boots with defaults (`ENABLE_RERANK is False`, `RERANK_DEVICE == "cpu"`,
`RERANK_TOP_N == 5`) and honours env overrides; every existing F3/F4/F5 test still passes
(additive-keys proof).

### T2 — Cross-encoder singleton (CPU-pinned, off-loop, shared)
Implement `rerank.get_rerank_model(settings)` — module-level singleton guarded by an
`asyncio.Lock`, constructed once via `HuggingFaceCrossEncoder(model_name=settings.RERANK_MODEL,
model_kwargs={"device": settings.RERANK_DEVICE})` inside `anyio.to_thread.run_sync` (AC-1/AC-2). Add
`warm_rerank_model(settings)`.
**Test:** patched constructor is called **once** across two concurrent `get_rerank_model` calls
(singleton + lock, AC-2); `model_kwargs={"device": "cpu"}` asserted (AC-1); load runs through
`anyio.to_thread.run_sync` (spy on the offload, AC-22).

### T3 — Batched scoring offload (direct path)
In `rerank_chunks`, build `(query, text)` pairs for the pool and score them in **one** batched call
via `logits = await anyio.to_thread.run_sync(model.score, pairs)` (AC-4/AC-7).
**Test (mocked model.score):** a 12-chunk pool produces exactly **one** `score` call with 12 pairs
(not 12 calls, AC-7); scoring is dispatched through `anyio.to_thread.run_sync` (spy, AC-4).

### T4 — Calibration (sigmoid, verified activation)
Implement `_calibrate(logits, settings)`: `sigmoid` into `[0, 1]` when `RERANK_APPLY_SIGMOID`, else
pass through (AC-10/AC-11). Inline pure-CPU.
**Test:** a positive and a negative logit map into `[0, 1]` preserving order under
`RERANK_APPLY_SIGMOID=true`; under `false` the values pass through unchanged; a large positive logit
→ ~1.0, large negative → ~0.0.

### T5 — Reorder whole objects + bind score + slice (AC-9/AC-6)
Bind each calibrated score onto its **whole** `RetrievedChunk` (`chunk.rerank_score = score`), sort
the objects by `rerank_score` desc, slice top `RERANK_TOP_N`. Never sort parallel arrays. Populate
`rerank_ms`; empty pool short-circuits (`[]`, `max_rerank_score=0`, `rerank_ms=0`, no model call,
AC-14); whitespace/empty text guarded via `_safe_text` (AC-15).
**Test:** synthetic candidates with known mocked logits reorder so the highest-logit chunk is #1 and
the output length is `RERANK_TOP_N` (AC-6); after slice each chunk's `rerank_score`, `chunk_id`, page
metadata and text still belong together (no re-zip drift, AC-9); empty pool → `[]` with **no** model
call (AC-14); a whitespace-only chunk does not break the batch (AC-15).

### T6 — Verify activation (sanity check) + pin calibrated range
Run the `design.md §5` sanity check against the real model: score one clearly-relevant and one
clearly-irrelevant pair; confirm raw-logit vs already-activated and set `RERANK_APPLY_SIGMOID`
accordingly (default `true` for `ms-marco-MiniLM-L-6-v2`). Add `rerank.max_rerank_score(chunks)`.
**Test:** with the real (or a faithfully-stubbed) model, the relevant pair scores materially higher
than the irrelevant one and the calibrated scores sit in `[0, 1]`; `max_rerank_score` returns the top
chunk's score (0.0 on empty), pinning the range so a future activation change fails loudly (AC-11).

### T7 — Seam step + `pool_k` widening (retriever.retrieve)
In `retriever.py`: gather the candidate pool per mode (hybrid → `hybrid_retrieve` ≤12; dense_only /
bm25_only → their top-`pool_k` where `pool_k = RERANK_CANDIDATE_K if ENABLE_RERANK else k`), then
`if settings.ENABLE_RERANK: return await rerank.rerank_chunks(query, pool, settings)` else
`return pool[:k]`. Signature unchanged (AC-19). `rerank` imported lazily (import-cycle safety).
**Test:** with `ENABLE_RERANK=false`, `retrieve` returns exactly the F5 `pool[:k]` result (same
chunks/order) for a fixed mocked pool (byte-for-byte F5 parity, AC-17); with `ENABLE_RERANK=true`
(hybrid) it returns `rerank_chunks(...)` (reranked top-N, `rerank_score` populated); the retrieval
suite path (`retrieve` called directly) re-measures the reranked order with no scoring change
(AC-18).

### T8 — Toggle overlay + gate swap
Extend `rag/flags.apply_flags` to also map `flags.rerank -> ENABLE_RERANK` (`design.md §9`). Change
`refusal.pre_llm_gate` so that with `ENABLE_RERANK` on it refuses when
`max_rerank_score(chunks) < settings.REFUSAL_RERANK_THRESHOLD`, else keeps the F5 max-`dense_score`
gate (AC-12).
**Test:** `apply_flags(settings, PipelineFlags(rerank=True)).ENABLE_RERANK is True` and the original
`settings` is unmutated; with rerank on, a pool whose `max_rerank_score` is below threshold refuses
and one above does not (AC-12); with rerank off, the F5 dense-cosine gate is used unchanged and every
existing F3/F5 refusal test still passes.

### T9 — LangChain API surface (off the runtime path, AC-3)
Implement `build_compression_retriever(base_retriever, settings)` returning
`ContextualCompressionRetriever(base_compressor=CrossEncoderReranker(model=<get_rerank_model result>,
top_n=RERANK_TOP_N), base_retriever=base_retriever)`, and a `HybridBaseRetriever(BaseRetriever)`
adapter over `hybrid.hybrid_retrieve` (RetrievedChunk → Document) so it has a base retriever.
**Test:** the compression retriever is built over the **same** model instance the direct path uses
(identity assert) and returns `top_n` documents on a mocked pool; a request-path test asserts
`ContextualCompressionRetriever`/`compress_documents` are **never** called during `answer()`
(off-path guard, AC-3).

### T10 — `rerank_ms` logging + async grep-guard + no-migration
Add `observability.log_rerank(rerank_ms, max_score, n_candidates)` (structlog, mirroring
`log_llm_cost`) and call it from `rerank_chunks` (AC-20). Extend the `app/rag/` async grep-guard to
cover `rerank.py` (no `\.invoke`, `embed_query`, blocking `requests`, sync `redis`). Confirm
`alembic revision --autogenerate` is an empty diff (AC-22/AC-23).
**Test:** a rerank logs one `rag.rerank` record carrying `rerank_ms`/`max_rerank_score`/count; the
grep-guard covers `rerank.py` and is green; `alembic check`/autogenerate yields no new migration.

### T11 — Loop-lag probe + latency (< 300 ms p50)
Add the loop-lag probe: schedule a concurrent tick while a rerank runs and assert its scheduling lag
stays within threshold (AC-5) — the explicit `anyio.to_thread.run_sync` offload is what makes it
pass. Add a latency check: a 12-pair rerank completes in **< 300 ms CPU (p50)** and records
`rerank_ms` (AC-8).
**Test:** the loop-lag probe is green (the loop kept ticking during the rerank, AC-5); p50 of a
repeated 12-pair rerank < 300 ms on CI CPU (AC-8). (Mark the latency assertion tolerant of CI
variance but record the measured p50 in the eval report.)

### T12 — Acceptance / definition of done (unit + integration)
Wire the `requirements §4` acceptance suite end-to-end (mocked cross-encoder + fixed fused pool):
rerank ordering + metadata binding (T5), calibration + activation verification (T4/T6), gate swap
(T8), empty/degenerate input (T5), the LangChain API-surface + off-path guard (T9), the loop-lag
probe (T11), and F5 toggle parity (T7). Add a rerank smoke test: a query where an off-topic chunk
tops RRF but the on-topic chunk is promoted to #1 by the cross-encoder.
**Definition of done:** `pytest tests/rag/` green including all `requirements §4` tests and the async
grep-guard; `ENABLE_RERANK=false` proven identical to `f5-hybrid-after`; no Alembic migration added;
`rerank_ms` logged.

### T13 — EVAL GATE (mandatory Phase-B closer)
Run the F4 harness against the rerank path and commit the delta report:

```bash
# same dense index + bm25.pkl as f5-hybrid-after (same SHA/manifest); F6 adds no re-index
# (design §2) — the fused pool is identical, so f5-hybrid-after numbers stay comparable.

python -m app.evals.run --suite all \
    --flags hybrid=on,rerank=on,query_rewrite=off,compression=off,memory=off \
    --label f6-rerank-after --yes

python -m app.evals.run --label f6-rerank-after --compare f5-hybrid-after
```

Then commit `docs/eval_results/f6-rerank-after.md` and
`docs/eval_results/f6-rerank-after-vs-f5-hybrid-after.md`.

The refusal suite tunes `REFUSAL_RERANK_THRESHOLD` (AC-13): sweep the threshold on the F4 refusal
suite, pick the value where recall rises without worsening the false-refusal rate, record it in the
report, and set it as the Settings default.

**Definition of done (the gate):** the delta table exists and is committed, mapping `f6-rerank-after`
→ its git SHA + index manifest; the headline expectation is **`context_precision` and `faithfulness`
up** (RAGAS) and **refusal-suite recall up with false-refusal not worse**, reported **overall and per
slice**; `rerank_ms` p50 (< 300 ms) and the loop-lag probe result are noted. Per CLAUDE.md, **the
feature is not done until this delta table is committed.**

---

**Gate label sequence (fixed):** `baseline` → `f5-hybrid-after` → **`f6-rerank-after`** →
`f7-rewrite-after` → … F6's "before" is the `f5-hybrid-after` report; F7's "before" will be
`f6-rerank-after`. Every README benchmark row for reranking maps to the `f6-rerank-after` label,
which maps to a git SHA + index manifest, so all numbers are reproducible.
