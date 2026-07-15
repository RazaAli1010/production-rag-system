# F6 — Cross-Encoder Reranking · requirements.md

**Module:** `backend/app/rag/rerank.py` (+ a rerank step in the `retriever.retrieve` seam body)
**Phase:** B (retrieval enhancement #2) · **Depends on:** F5 (fused top-12 pool + `RetrievedChunk`
scores), F4 (eval harness) · **Flag:** `ENABLE_RERANK`
**Eval gate:** `f5-hybrid-after` → **`f6-rerank-after`** (second `--compare` gate in the fixed
sequence).

---

## 1. Overview

F5 hands generation a fused **top-12 candidate pool** ranked by RRF over dense + BM25 ranks. RRF is
a *rank-only* fusion: it knows a chunk placed highly in some list, but it never actually reads the
chunk text against the query. F6 adds the **precision layer**: a cross-encoder
(`cross-encoder/ms-marco-MiniLM-L-6-v2`) jointly encodes each `(query, chunk_text)` pair and emits a
relevance logit, so the model reads the query *and* the candidate together and can tell a
genuinely-on-topic passage from a lexically-similar-but-wrong one. F6 reranks the 12 candidates,
keeps the best **5** for generation, and derives a **calibrated confidence** (sigmoid over the top
logit) that **replaces the crude v1 dense-cosine refusal gate** with a signal tuned on the F4
refusal suite.

F6 sits **inside** the F3→F5 `retrieve(query, k, namespace, settings) -> list[RetrievedChunk]`
seam, exactly where F5 lives — it consumes the fused pool F5's `hybrid_retrieve` already exposes for
this purpose (F5 US-6) and re-orders it *before* the seam truncates to `k`. The prompt, context
formatting, citation parsing, and SSE contract are untouched. Because rerank lives in the seam, the
F4 **retrieval suite** (which calls `retrieve` directly) re-measures hit@k/MRR over the reranked
order with **zero F4 code change** — the same property F5 relied on.

F6 is fully toggleable: `ENABLE_RERANK=false` (default) is byte-for-byte the F5 path (fused
`[:k]`, dense-cosine refusal gate); `true` inserts the cross-encoder and switches the gate to the
calibrated rerank score. The F4 harness drives the same toggle via `--flags rerank=on/off`.

### 1.1 Design decisions resolved in the feature brief (do NOT re-derive)

- **The direct-call path is the runtime path.** The generation-critical rerank scores the
  cross-encoder **directly** (`HuggingFaceCrossEncoder.score(pairs)`), because that is the only way
  to obtain the raw per-pair scores needed to populate `RetrievedChunk.rerank_score` and drive the
  calibrated-confidence gate. LangChain's `CrossEncoderReranker.compress_documents` reranks and
  slices but **discards the scores**, so it cannot be the source of the confidence signal.
- **`ContextualCompressionRetriever` is required API surface, NOT the request path.** It re-runs the
  base retriever and *then* compresses (i.e. it re-retrieves); F5 has already produced the fused
  top-12, so putting it on the request path would retrieve twice. It is built over the **same loaded
  model instance** (zero extra memory), exercised by a unit test, and never invoked during
  generation.
- **One model load, shared.** The cross-encoder is instantiated once as a process singleton, pinned
  to CPU, and the *same* object backs both the direct scoring path and the LangChain
  `CrossEncoderReranker`. No second copy of the weights in memory.

---

## 2. User stories

- **US-1 (Student — precision):** As a student whose question lexically overlaps several clauses
  ("attendance shortage ki waja se exam se rok"), I want the passage that actually answers me ranked
  first, not merely the one sharing the most keywords, so the cited clause is the correct one.
- **US-2 (Student — honest refusal):** As a student asking something the corpus does not cover, I
  want a refusal driven by a *calibrated* relevance score (not a raw cosine that RRF can inflate),
  so a plausible-looking-but-irrelevant top hit does not produce a confident wrong answer.
- **US-3 (Ops / cost owner):** As the person paying the bill, I want rerank behind a single flag
  (`ENABLE_RERANK`) with the F5 path preserved verbatim, so I can A/B it and roll back in prod
  instantly if reranking regresses a slice or blows the latency budget.
- **US-4 (Eval author):** As the person running the gate, I want to A/B `hybrid` vs `hybrid+rerank`
  under the F4 harness with **zero F4 code change**, so context_precision / faithfulness / refusal
  deltas are directly comparable to `f5-hybrid-after`.
- **US-5 (Reliability / latency owner):** As the on-call, I want reranking to stay CPU-bound,
  off-loop, and under a hard latency budget (12 pairs < 300 ms p50), so token streaming and
  concurrent requests are never stalled by the forward pass.
- **US-6 (JD-relevant API surface):** As the reviewer of the LangChain integration, I want a
  working `ContextualCompressionRetriever(CrossEncoderReranker(...))` built over the shared model
  and covered by a test, so the LangChain reranking API is demonstrably exercised even though it is
  off the hot path.
- **US-7 (Downstream augmentation/generation developer):** As the author of a later
  augmentation/generation stage (or F9/F17), I want F6 to leave the seam signature and the SSE
  contract unchanged and to populate `rerank_score` on the returned chunks, so later stages consume
  the reranked top-5 without touching rerank code.

---

## 3. EARS acceptance criteria

### 3.1 Model loading (singleton, CPU-pinned, shared)
- **AC-1 (Ubiquitous):** The system shall load the cross-encoder via
  `HuggingFaceCrossEncoder(model_name=settings.RERANK_MODEL, model_kwargs={"device": "cpu"})`
  **exactly once** per process (module-level singleton), pinning `device="cpu"` explicitly so the
  model never auto-selects CUDA/MPS on a dev/CI machine with a GPU or Apple silicon.
- **AC-2 (Ubiquitous):** The model construction (weight load, blocking/CPU-bound, possibly a
  first-use download) shall be offloaded via `anyio.to_thread.run_sync` and guarded so concurrent
  first requests load it only once, per the CLAUDE.md async mandate (no blocking load on the loop).
- **AC-3 (Ubiquitous — API surface, JD-relevant):** The system shall construct
  `ContextualCompressionRetriever(base_compressor=CrossEncoderReranker(model=<the shared
  HuggingFaceCrossEncoder>, top_n=settings.RERANK_TOP_N), base_retriever=<a BaseRetriever adapter
  over F5's hybrid_retrieve>)`, passing the **same** model instance used by the direct path, cover
  it with a unit test, and **never invoke it on the request/generation path**.

### 3.2 Async responsiveness (hard requirement)
- **AC-4 (Ubiquitous):** The system shall score all candidate pairs with an **explicit**
  `logits = await anyio.to_thread.run_sync(hf_model.score, pairs)` — the direct offload in our own
  code, not LangChain's opaque `acompress_documents` executor fallback — so the blocking forward
  pass runs on a worker thread and the event loop keeps streaming tokens and serving concurrent asks.
- **AC-5 (Event-driven — loop-lag probe):** When a rerank runs, a concurrently-scheduled event-loop
  tick shall observe scheduling lag within a bounded threshold (the probe test), proving the
  forward pass did not block the loop; the explicit `anyio.to_thread.run_sync` offload is what makes
  this pass.

### 3.3 Rerank flow & latency
- **AC-6 (Ubiquitous):** The system shall take F5's fused candidate pool (up to
  `HYBRID_FUSED_TOP_K`, default 12), rerank it, and return the top `RERANK_TOP_N` (default 5) to
  generation — the count handed to generation stays 5.
- **AC-7 (Ubiquitous):** The system shall score the whole pool in a **single** `score(pairs)` call
  (batched, not one call per pair, AC-7 edge case in the brief).
- **AC-8 (Ubiquitous — latency):** A 12-pair rerank shall complete in **< 300 ms CPU (p50)**, and
  the elapsed time shall be recorded as `rerank_ms`.
- **AC-9 (Ubiquitous — metadata binding):** The system shall reorder **whole `RetrievedChunk`
  objects** (scores + metadata travelling with the object), never sort parallel arrays that are
  later re-zipped, so a chunk's `rerank_score`, citation metadata, and text stay bound to it through
  the sort **and** the top-N slice.

### 3.4 Calibrated confidence & refusal gate
- **AC-10 (Ubiquitous):** The system shall map each raw logit to a calibrated score in `[0, 1]` via
  a sigmoid, populate it on `RetrievedChunk.rerank_score`, and expose `max_rerank_score` (the top
  reranked chunk's score) as the confidence signal.
- **AC-11 (Ubiquitous — verify activation before trusting calibration):** Before applying the
  sigmoid, the implementation shall confirm what `HuggingFaceCrossEncoder.score` /
  `CrossEncoder.predict` returns for this model — raw logits vs. an already-activated value — via a
  sanity check on one clearly-relevant and one clearly-irrelevant pair (logits are unbounded and can
  be negative; probabilities sit in `[0, 1]`). If the output is already in `[0, 1]`, the double
  sigmoid shall be dropped and the score treated as calibrated directly. The behaviour is controlled
  by `RERANK_APPLY_SIGMOID` so the verified choice is explicit and testable.
- **AC-12 (State-driven — gate swap):** While `ENABLE_RERANK` is `true`, the pre-LLM refusal gate
  shall refuse when `max_rerank_score < REFUSAL_RERANK_THRESHOLD` (the calibrated gate replacing the
  v1 dense-cosine gate); while `false`, the gate shall behave exactly as F5 (max `dense_score` <
  `REFUSAL_DENSE_THRESHOLD`), so rerank-off is byte-for-byte the F5 refusal path.
- **AC-13 (Ubiquitous — threshold tuned, not guessed):** `REFUSAL_RERANK_THRESHOLD` shall be a
  central Settings value tuned against the F4 **refusal suite** (recall up, false-refusal rate not
  worse), not an arbitrary constant.

### 3.5 Edge cases
- **AC-14 (Unwanted — empty pool):** If F5 returns zero chunks, the system shall short-circuit
  (return `[]`, `max_rerank_score = 0`, `rerank_ms = 0`) **without** calling the model, falling into
  the existing empty-retrieval → refusal path.
- **AC-15 (Unwanted — degenerate chunk text):** The system shall guard whitespace-only or empty
  `page_content`/`text` before scoring so a degenerate pair cannot produce a garbage score or break
  the batch (e.g. substitute a safe placeholder or floor its score).
- **AC-16 (Ubiquitous — offline weights, coordinate F15):** The cross-encoder weights shall be
  pre-downloadable at Docker build time so there is no runtime network dependency, with
  `HF_HUB_OFFLINE=1` enforceable at runtime. This is a Docker/env concern (F15), **not** a Settings
  value; local dev caches weights to `~/.cache/huggingface` on first use.

### 3.6 Toggling, contracts, observability & scope
- **AC-17 (State-driven — prod/request toggle):** While `ENABLE_RERANK` is `false` (default), the
  system shall behave byte-for-byte as the F5 path (same chunks/order/refusal as
  `f5-hybrid-after`); while `true`, it shall rerank and switch the gate.
- **AC-18 (Ubiquitous — request/eval flag):** The system shall map `PipelineFlags.rerank` onto
  `ENABLE_RERANK` via the **same** `rag.flags.apply_flags` overlay F5 introduced (extended by one
  key), applied at the same two call sites (`baseline._pipeline_events` and
  `evals.retrieval.run_retrieval`) — so rerank is toggleable per request/eval with no new wiring and
  the retrieval suite re-measures the reranked order with zero F4 change.
- **AC-19 (Ubiquitous — seam & SSE unchanged):** The system shall keep the seam signature identical
  (`retrieve(query, k, namespace, settings) -> list[RetrievedChunk]`), add **no** new SSE stage
  (reranking is internal to the existing `searching` stage, mirroring F5's "no new stage"
  decision), and leave prompt/`format_context`/`parse_citations`/citation-mapping untouched.
- **AC-20 (Ubiquitous — score populated + metric logged):** The system shall populate `rerank_score`
  on every returned `RetrievedChunk` and log `rerank_ms` (and `max_rerank_score`, candidate count)
  via the central observability path (structlog now; flows to `request_logs`/Langfuse when F13 wires
  the central request logger, mirroring F3/F5 cost/degraded logging).
- **AC-21 (Ubiquitous — Settings):** Every new configuration value (`ENABLE_RERANK`, `RERANK_MODEL`,
  `RERANK_TOP_N`, `RERANK_CANDIDATE_K`, `RERANK_DEVICE`, `RERANK_APPLY_SIGMOID`,
  `REFUSAL_RERANK_THRESHOLD`) shall live in the central `app.core.settings.Settings` class;
  `HYBRID_FUSED_TOP_K` is reused as the pool size, not redefined.
- **AC-22 (Ubiquitous — async mandate):** The system shall keep the rerank path async end-to-end:
  the only offloads are the one-time model load and the per-request `score` call (both via
  `anyio.to_thread.run_sync`); sigmoid math over ≤12 floats runs inline as cheap pure-CPU. No sync
  twin (`invoke`, `embed_query`, blocking `requests`, sync `redis`) appears in `rerank.py`.
- **AC-23 (Ubiquitous — no migration):** F6 shall add no Alembic migration — `RetrievedChunk`
  already reserves `rerank_score` (contracts.py "F5/F6 populate … without a schema change"),
  `AnswerResponse` is unchanged, and no table is added.
- **AC-24 (Ubiquitous — eval gate):** The system's definition of done shall include running the F4
  `--suite all` harness with `--flags hybrid=on,rerank=on --label f6-rerank-after`, then
  `--compare f5-hybrid-after`, and committing the resulting
  `docs/eval_results/f6-rerank-after-vs-f5-hybrid-after.md` delta report.

---

## 4. Acceptance criteria (feature-level definition of done)

1. **Rerank ordering** is unit-tested: on synthetic candidates with known relevance, the
   cross-encoder-selected top-5 differs from the RRF order and puts the on-topic chunk first;
   `rerank_score` is populated and monotonic with the mocked logits (AC-6/AC-9/AC-10).
2. **Metadata binding** is unit-tested: after rerank + top-N slice, each returned chunk's
   `rerank_score`, `chunk_id`, page metadata, and text still belong to the same chunk — no parallel
   array re-zip drift (AC-9).
3. **Calibration** is unit-tested: `RERANK_APPLY_SIGMOID=true` maps a positive and a negative logit
   into `[0, 1]` and preserves order; the activation-verification sanity check (relevant vs
   irrelevant pair) is asserted (AC-10/AC-11).
4. **Gate swap** is unit-tested: with rerank on, a pool whose `max_rerank_score` is below threshold
   refuses and one above does not; with rerank off, the F5 dense-cosine gate is used unchanged
   (AC-12). Existing F5/F3 refusal tests still pass.
5. **Empty / degenerate input** is unit-tested: empty pool short-circuits with no model call,
   `max_rerank_score=0`, `rerank_ms=0` (AC-14); a whitespace-only chunk is guarded, not fatal
   (AC-15).
6. **Loop-lag probe** passes: event-loop scheduling lag during a rerank stays within threshold
   (AC-5); **latency** p50 for a 12-pair rerank < 300 ms and `rerank_ms` is recorded (AC-8).
7. **LangChain API surface** is unit-tested: `ContextualCompressionRetriever` +
   `CrossEncoderReranker` are built over the shared model instance and return `top_n` docs; asserted
   **not** called on the request path (AC-3).
8. **Toggle parity** is asserted: with `ENABLE_RERANK=false`, `retrieve` returns exactly the F5
   `fused[:k]` result (same chunks/order) for a fixed mocked pool (AC-17); `apply_flags` maps
   `flags.rerank` and the retrieval suite re-measures the reranked order with no scoring change
   (AC-18).
9. **Async grep-guard** covers `rerank.py` (no sync twin); `alembic` autogenerate is empty (AC-22/
   AC-23).
10. **Eval gate:** `docs/eval_results/f6-rerank-after.md` and
    `docs/eval_results/f6-rerank-after-vs-f5-hybrid-after.md` are committed; the delta table shows
    the headline expectation — `context_precision` and `faithfulness` up, refusal-suite recall up
    with false-refusal not worse — overall and per slice (AC-13/AC-24).
11. Every AC above is covered by an automated test — this list is the test list, not aspiration.

---

## 5. Out of scope (do not implement here)

- **Caching (F9), memory (F17), and the later augmentation/generation phase:** F6 touches
  only the rerank step inside the retrieval seam; the F9 cache key and all downstream flags are
  untouched. (The former F7 query-rewrite / F8 compression stages have been dropped — retrieval
  enhancement ends at F6.)
- **A GPU / batched-service reranker:** F6 is CPU, in-process, single `score` call; a GPU path or a
  separate reranking microservice is not part of this gate.
- **Changing F5 fusion, `bm25.pkl`, or the dense index:** F6 reads F5's fused pool as-is and forces
  **no** re-index/re-embed, so `f5-hybrid-after` numbers stay comparable (blast-radius note in
  design §2).
- **A second reranker model / model A-B:** only `cross-encoder/ms-marco-MiniLM-L-6-v2` is wired;
  swapping models is a later tuning, not this gate.
- **Wiring the SSE `reranking` stage / F11 startup lifespan preload:** F6 adds no new stage
  (AC-19) and provides a `warm_rerank_model` hook, but the FastAPI lifespan that calls it at startup
  is F11's job — F6 lazy-loads the singleton so correctness holds without it.
- **Persisting `rerank_score` / `rerank_ms` to `request_logs`:** the central request-log writer is
  F13; F6 logs the metric via structlog (the F3/F5 convention) so F13 wires it in without an F6
  change.
