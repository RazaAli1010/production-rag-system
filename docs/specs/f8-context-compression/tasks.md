# F8 — Context Compression & Token Cost Control · tasks.md

**Module:** `backend/app/rag/compression.py` (post-refusal / pre-generation step in
`_pipeline_events`) · **Depends on:** F6, F7, F4 · **Flag:** `ENABLE_COMPRESSION` · **Eval gate:**
`f7-rewrite-after` → `f8-compression-after`
**No new model / no OpenAI call:** sentence scoring reuses `rerank.get_rerank_model`; token counting is
tiktoken `cl100k_base`.

Each task is scoped to ≈ ≤ 1 hour and carries its own test criterion. Ordering follows the data flow:
settings → dedupe → floor → sentence trim → budget fill → orchestrator → pipeline wiring → toggle →
metrics → LangChain surface → guards → acceptance → **eval gate**.

The final task **is** the mandatory Phase-B gate: run F4 `--suite all` with
`hybrid=on,rerank=on,query_rewrite=on,compression=on`, `--compare f7-rewrite-after`, and commit the
delta reports to `docs/eval_results/`.

---

### T1 — Settings (F8 keys)
Add the F8 keys from `design.md §7` to `Settings` (`ENABLE_COMPRESSION=False`,
`COMPRESSION_SCORE_FLOOR=0.25`, `COMPRESSION_MIN_CHUNKS=2`, `COMPRESSION_TOKEN_BUDGET=2200`,
`COMPRESSION_DEDUPE_JACCARD=0.7`, `COMPRESSION_DEDUPE_NGRAM=5`). `ENABLE_COMPRESSION` joins the
feature-flag block; reuse `RERANK_MODEL`/`RERANK_DEVICE`/`RERANK_APPLY_SIGMOID` (do not redefine).
**Test:** `Settings()` boots with defaults (`ENABLE_COMPRESSION is False`, `COMPRESSION_SCORE_FLOOR ==
0.25`, `COMPRESSION_TOKEN_BUDGET == 2200`, `COMPRESSION_MIN_CHUNKS == 2`) and honours env overrides;
every existing F3–F7 test still passes (additive-keys proof).

### T2 — `count_tokens` + dedupe (5-gram Jaccard) (AC-4/AC-5)
Create `app/rag/compression.py` with `_ENC = tiktoken.get_encoding("cl100k_base")`,
`count_tokens(text)`, `_ngrams(text, n)`, `_jaccard(a, b)`, and
`dedupe(chunks, settings) -> (kept, n_dropped)`: walk in rerank order, drop a chunk whose 5-gram
Jaccard vs an already-kept higher-or-equal-scored chunk exceeds `COMPRESSION_DEDUPE_JACCARD`; a chunk
with `< NGRAM` words compares by its full word-set; never drop below `COMPRESSION_MIN_CHUNKS`.
**Test:** two chunks with Jaccard `> 0.7` collapse to the higher-`rerank_score` one; a below-threshold
pair is untouched; a 3-word chunk compares without crashing; a set of all-identical chunks stops at
`MIN_CHUNKS`.

### T3 — Relevance floor (AC-1/AC-2/AC-3)
Implement `_score_of(chunk)` and `relevance_floor(chunks, settings) -> (kept, n_dropped)`: keep chunks
with `rerank_score >= COMPRESSION_SCORE_FLOOR`; a chunk with `rerank_score is None` is always kept
(AC-3); if fewer than `COMPRESSION_MIN_CHUNKS` survive, top up from the dropped set by descending score.
**Test:** chunks below `0.25` are dropped; a set where only 1 clears the floor still returns 2
highest-scored chunks; a `rerank_score=None` chunk is never floored; an input already ≤ `MIN_CHUNKS` is
returned whole.

### T4 — Sentence split + single-chunk trim (AC-8/AC-10)
Implement `_split_sentences(text)` (regex on sentence terminators, keep identifiers like `15(3)`
intact, drop empties, preserve order) and
`async _trim_chunk(query, chunk, budget, settings) -> (RetrievedChunk, n_dropped)`: if the whole chunk
fits `budget`, return it unchanged; else score every `(query, sentence)` pair in **one** batched
`await anyio.to_thread.run_sync(model.score, pairs)` (model from `rerank.get_rerank_model`), greedily
keep the highest-scored sentences whose cumulative tokens fit `budget`, re-emit them in **original
order**, and return a `model_copy` with only `text` replaced (metadata + scores preserved).
**Test (mocked cross-encoder):** a chunk over budget is trimmed so kept sentences are the top-scored,
re-emitted in document order, total tokens ≤ budget; a chunk already under budget is returned unchanged
(no model call); the trimmed copy keeps `chunk_id`/`doc_id`/`page_start`/`page_end`/`rerank_score`; a
single over-budget sentence is still kept (never empty `text`); the score call is one batched off-loop
call.

### T5 — Token-budget greedy fill (AC-6/AC-7)
Implement `async token_budget_fill(query, chunks, settings) -> (kept, n_sentences_dropped)`: greedy-add
chunks in rerank order while cumulative tokens ≤ `COMPRESSION_TOKEN_BUDGET`; the first overflow chunk is
`_trim_chunk`-ed to the remaining budget and chunks after it are dropped; the top
`COMPRESSION_MIN_CHUNKS` chunks are always retained (trimmed if needed) so the floor guarantee survives.
**Test (mocked cross-encoder):** chunks that fit are added whole; the overflow chunk is trimmed (not
dropped); chunks after the overflow are dropped; when chunk 1 alone exceeds the budget, chunks 1..MIN
are still retained (trimmed); `n_sentences_dropped` reflects the trim.

### T6 — `compress_chunks` orchestrator + fallback (AC-12/AC-13)
Implement `async compress_chunks(query, chunks, settings)`: empty input → return as-is; compute
`tokens_before`; run `dedupe → relevance_floor → token_budget_fill` inside a try/except; on any
exception log `compression_failed` and return the **original** chunks; else compute `tokens_after` and
call `observability.log_compression(...)`; return the compressed list.
**Test:** a fixed pool compresses (tokens_after < tokens_before, one `rag.compression` record); an
injected exception in the trim path yields the original chunks + a logged `compression_failed`, never
raising; empty input returns `[]` with no log.

### T7 — Observability `log_compression` (AC-12)
Add `observability.log_compression(tokens_before, tokens_after, chunks_before, chunks_after,
sentences_dropped, compression_ms)` (structlog `rag.compression`, deriving `chunks_dropped =
chunks_before - chunks_after`), mirroring `log_rerank`/`log_rewrite`. No `estimate_cost` site is added
(AC-14).
**Test:** one call emits one `rag.compression` event carrying every field including `chunks_dropped`;
no `rag.llm_cost` is emitted by the compression path itself.

### T8 — Pipeline wiring in `_pipeline_events` (AC-9/AC-11/AC-16/AC-18)
In `baseline._pipeline_events`, after the `if refusal.pre_llm_gate(chunks, settings): … return` block
and before `chain_input` is built, add the flag-gated block from `design.md §4`:
`if settings.ENABLE_COMPRESSION: scoring_query = rewrite_result.normalized if rewrite_result else
query; chunks = await compression.compress_chunks(scoring_query, chunks, settings)`. Import
`compression` in `baseline.py`. No SSE stage is added.
**Test:** with `ENABLE_COMPRESSION=true`, an end-to-end `_pipeline_events` run compresses the chunks so
`format_context` and `parse_citations` both see the **same** compressed list (`[n]` maps 1:1, AC-11)
and the ordered SSE contract still holds with **no** new stage; a refused query never calls
`compress_chunks`; the scoring query is `rewrite_result.normalized` when rewrite ran (AC-9).

### T9 — Toggle parity: `ENABLE_COMPRESSION=false` ≡ f7 (AC-16)
Assert flag-off is byte-for-byte the `f7-rewrite-after` generation path.
**Test:** with `ENABLE_COMPRESSION=false`, `_pipeline_events` produces the same chunks/prompt/citations
as before F8 for a fixed mocked pool (no `compress_chunks` call, no `rag.compression` record); with a
disclaimer/refusal fixture the outputs match the pre-F8 snapshot.

### T10 — Toggle overlay (flags.apply_flags) (AC-17)
Extend `rag.flags.apply_flags` to also map `flags.compression -> ENABLE_COMPRESSION` (`design.md §9`).
Confirm `parse_flags("…,compression=on")` already yields the flag (no parser change).
**Test:** `apply_flags(settings, PipelineFlags(compression=True)).ENABLE_COMPRESSION is True` and the
input `settings` is unmutated; `parse_flags("hybrid=on,rerank=on,query_rewrite=on,compression=on").
compression is True` with `cache` still forced `False`.

### T11 — LangChain API surface + async grep-guard + no-migration (AC-15/AC-20 / FR2)
Implement `build_document_compressor(settings)` returning a `DocumentCompressorPipeline` that stacks the
F6 `CrossEncoderReranker` (over the shared `get_rerank_model` instance, `top_n=RERANK_TOP_N`) with the
F8 filters as `BaseDocumentCompressor`s — API surface only, never on the request path (mirrors
`rerank.build_compression_retriever`). Confirm the `app/rag/` async grep-guard in
`tests/rag/test_generation.py` covers `compression.py`; confirm `alembic revision --autogenerate` is
empty.
**Test:** `build_document_compressor(settings)` returns a `DocumentCompressorPipeline` over the same
loaded model and is not referenced by `_pipeline_events`; the grep-guard is green for `compression.py`
(no `\.invoke`/`embed_query`/blocking `requests`/sync `redis`); `alembic check`/autogenerate yields no
new migration.

### T12 — Acceptance / definition of done (unit + integration)
Wire the `requirements §4` acceptance suite end-to-end (mocked cross-encoder + fixed reranked pools):
floor (T3), dedupe (T2), budget + trim (T4/T5), citation safety + numbering (T4/T8), fallback (T6),
metrics (T7), toggle parity (T9/T10), LangChain surface (T11). Add a compression smoke test: a 5-chunk
reranked pool with one low-score filler chunk + one near-duplicate + one long chunk compresses to fewer
tokens with the answer-bearing chunk retained and citations intact.
**Definition of done:** `pytest tests/rag/` green including all `requirements §4` tests and the async
grep-guard; `ENABLE_COMPRESSION=false` proven identical to `f7-rewrite-after`; no OpenAI call added by
compression; no Alembic migration; `rag.compression` metrics logged.

### T13 — EVAL GATE (mandatory Phase-B closer)
Run the F4 harness against the compression path and commit the delta reports:

```bash
# same dense index + bm25.pkl as f7-rewrite-after (same SHA/manifest); F8 adds no re-index/re-embed
# (design §2) — compression is a pre-generation transform, so f7-rewrite-after retrieval numbers stay
# byte-for-byte comparable.

python -m app.evals.run --suite all \
    --flags hybrid=on,rerank=on,query_rewrite=on,compression=on,memory=off \
    --label f8-compression-after --yes

python -m app.evals.run --label f8-compression-after --compare f7-rewrite-after
```

Then commit `docs/eval_results/f8-compression-after.md` and
`docs/eval_results/f8-compression-after-vs-f7-rewrite-after.md`.

**Definition of done (the gate):** the delta reports exist and are committed, mapping
`f8-compression-after` → its git SHA + index manifest. The report shows:
- **prompt-token reduction ≥ 25%** (mean over the eval set, from the run's `rag.compression`
  `tokens_before`/`tokens_after`) — **else tune `COMPRESSION_SCORE_FLOOR` / `COMPRESSION_TOKEN_BUDGET`
  and document the deviation**;
- **RAGAS faithfulness drop ≤ 0.02** vs `f7-rewrite-after`;
- **context_precision reported** (expected flat-or-up, since compression drops low-relevance context);
- **cost/query down** (fewer generation input tokens) and the **p95 latency delta** noted;
- **retrieval hit@k / MRR unchanged** vs `f7-rewrite-after` (compression is post-retrieval — design §9).

Per CLAUDE.md, **the feature is not done until this delta table is committed.**

---

**Gate label sequence (fixed):** `baseline` → `f5-hybrid-after` → `f6-rerank-after` →
`f7-rewrite-after` → **`f8-compression-after`** → `f9-cache-after` → `f17-memory-after`. F8's "before"
is the `f7-rewrite-after` report; F9's "before" will be `f8-compression-after`. Every README benchmark
row for compression maps to the `f8-compression-after` label, which maps to a git SHA + index manifest,
so all numbers are reproducible.
