# F8 — Context Compression & Token Cost Control · requirements.md

**Module:** `backend/app/rag/compression.py` (a post-retrieval / pre-generation step in `_pipeline_events`)
**Phase:** B (retrieval enhancement #4, the last Phase-B gate) · **Depends on:** F6 (the calibrated
`rerank_score` and the loaded cross-encoder), F7 (the `normalized` query the compressor scores
against), F4 (eval harness) · **Flag:** `ENABLE_COMPRESSION`
**Model:** none — compression reuses the **already-loaded** F6 cross-encoder
(`cross-encoder/ms-marco-MiniLM-L-6-v2`, `settings.RERANK_MODEL`) for sentence scoring and tiktoken
`cl100k_base` for token counting. **No OpenAI call is added.**
**Eval gate:** `f7-rewrite-after` → **`f8-compression-after`** (fourth `--compare` gate in the fixed
sequence).

---

## 1. Overview

F5/F6/F7 decide *which* chunks reach the LLM; they never trim *how much* of each chunk is sent. After
F6 truncates to `RERANK_TOP_N = 5`, the generation prompt still carries five whole chunks verbatim —
often with a long tail of low-relevance chunks, near-duplicate overlapping fixed-size chunks, and
paragraphs the answer never uses. Every one of those tokens is billed on **every** `gpt-4o-mini`
generation call and inflates latency for a student on a phone.

F8 inserts one **post-retrieval, pre-generation** compression step that shrinks the prompt context
**without a new model call and without hurting faithfulness — measured, not assumed**:

1. **Relevance floor** — drop reranked chunks whose calibrated `rerank_score` is below a floor
   (default `0.25`), but never leave fewer than `COMPRESSION_MIN_CHUNKS = 2` chunks for a
   non-refused query.
2. **Dedupe** — 5-gram Jaccard `> 0.7` between two chunks drops the lower-scored one (the overlapping
   fixed-window chunks F2 produces are the target).
3. **Token budget** — greedy-fill by rerank order up to `COMPRESSION_TOKEN_BUDGET = 2200` tokens; the
   single chunk that overflows the budget is **sentence-trimmed** — its sentences are scored against
   the query with the **same F6 cross-encoder** (batched, off-loop) and only the top sentences that
   fit are kept, in original order.
4. **Citation-safe** — a trimmed chunk keeps its full citation metadata (`doc_id`, `title`,
   `section_heading`, `page_start`/`page_end`, `anchor`); only `chunk.text` shrinks, so
   `extract_quote` still quotes verbatim stored text and the page mapping is preserved.

Compression is a **generation-path** transform, not a retrieval-algorithm change: `retriever.retrieve`,
`rewrite.retrieve`, `hybrid_retrieve`, `rerank_chunks`, `bm25.pkl`, and the dense index are all
untouched, so `f7-rewrite-after` retrieval numbers stay byte-for-byte comparable. F8 lives in a new
module `app/rag/compression.py` and slots into `baseline._pipeline_events` **after the refusal gate
and before the generation chain** (exactly the CLAUDE.md pipeline order: `… rerank → refusal gate →
compress → generate …`), behind the `ENABLE_COMPRESSION` flag: **flag off (default) is byte-for-byte
the `f7-rewrite-after` generation path** (no compression call, the same five chunks reach the prompt);
flag on runs dedupe → floor → budgeted fill/trim. The F4 harness drives the toggle via
`--flags compression=on/off`.

A compression failure (unexpected exception in the trim/score path) **never blocks answering** — the
pipeline falls back to the uncompressed chunk list and logs `compression_failed`, mirroring F7's
never-block rewrite philosophy.

### 1.1 Design decisions resolved in the feature brief (do NOT re-derive)

- **Compression runs on the generation path, NOT inside the retrieval seam.** The CLAUDE.md pipeline
  order is `rerank → refusal gate → compress → generate`: compression must run **after** the refusal
  gate (so a refusal decision is made on the full reranked confidence, never on a floored/trimmed set)
  and **before** the LCEL generation chain. It therefore lives in `_pipeline_events` between
  `refusal.pre_llm_gate` and `chain_input`, **not** in `rewrite.retrieve`/`retriever.retrieve`. A
  direct consequence: the F4 **retrieval suite** (which drives the `retrieve` seam directly and never
  reaches generation) is **untouched by compression** — F8's hit@k / MRR are identical to
  `f7-rewrite-after`. F8 is measured by the **RAGAS** suite (faithfulness, context_precision) and the
  **latency/cost** suite, not the retrieval suite. This is the structural difference from F5/F6/F7.
- **No new OpenAI call.** Sentence scoring reuses the F6 cross-encoder via `rerank.get_rerank_model`
  (one set of weights already in memory); token counting is tiktoken `cl100k_base`. So there is **no**
  `estimate_cost` site added for compression itself — the cost *win* is fewer `gpt-4o-mini` generation
  input tokens, logged through the existing `log_llm_cost(LLM_MODEL, tokens_in, …)` whose `tokens_in`
  already reflects the compressed context.
- **The scoring query is the F7 `normalized` query when rewrite ran, else the raw query.** The F6
  rerank scored the pool against `rr.normalized`; the sentence trimmer scores against the **same**
  query so the two relevance signals agree. `_pipeline_events` already reads `last_rewrite()`, so this
  needs no new plumbing.
- **The LangChain composable is API surface, not the runtime path (mirrors F6).** FR2 asks for "a
  stacked LangChain document compressor with F6 (one composable post-retrieval unit)." As in F6
  (`build_compression_retriever`), that is built as a `DocumentCompressorPipeline` stacking the F6
  `CrossEncoderReranker` with the F8 filters over the **same** loaded model, covered by a test but
  **never invoked on the request path** — the runtime path is our own `compress_chunks`, which keeps
  whole `RetrievedChunk` objects and their scores (LangChain's `Document` compressors discard the
  calibrated `rerank_score` the floor needs, the same reason F6/F7 rejected them on the hot path).
- **No new SSE stage (mirrors F5/F6/F7).** Compression is internal, run silently between the
  `searching` stage's `done` event and generation; the ordered `stage* → token* → citations → meta →
  done|error` contract is unchanged. F17 (owner of `app/memory/stages.py`) may later surface a
  dedicated `compressing` stage without touching F8.
- **`RetrievedChunk` gains no field; no migration.** Trimming mutates only `text` on a `model_copy`;
  citation metadata is preserved on the same object. `AnswerResponse` gains no field. F8 adds **no**
  Alembic migration (the compression metrics are structlog telemetry, routed to `request_logs`/Langfuse
  by F13).

---

## 2. User stories

- **US-1 (Ops / cost owner):** As the person paying the bill, I want the generation prompt trimmed of
  low-relevance and duplicate context so every `gpt-4o-mini` call bills fewer input tokens, with the
  saving measured at the gate (≥25% mean prompt-token reduction on the eval set).
- **US-2 (Student — faithfulness preserved):** As a student, I want compression to cut *filler*, not
  the clause that answers me, so my answer stays grounded and correctly cited (RAGAS faithfulness drop
  ≤ 0.02 at the gate).
- **US-3 (Student — cleaner citations):** As a student reading overlapping fixed-size chunks, I want
  near-duplicate chunks collapsed so I don't get the same passage cited twice under two `[n]` markers.
- **US-4 (Student — latency on mobile):** As a student on a phone, I want a shorter prompt so the
  first token arrives sooner (fewer input tokens to process), without losing citation accuracy.
- **US-5 (Ops — instant rollback / A-B):** As the person running prod, I want compression behind one
  flag (`ENABLE_COMPRESSION`) with the F7 generation path preserved byte-for-byte when off, so I can
  A/B the token saving against faithfulness and roll back instantly.
- **US-6 (Reliability owner):** As the on-call, I want any failure in the compression/trim path to
  fall back to the uncompressed chunks and still answer (logged `compression_failed`), never a blocked
  or errored response.
- **US-7 (Eval author):** As the person running the gate, I want to A/B `f7` vs `f7+compression` under
  the F4 harness with a single flag, and have the delta report the **prompt-token reduction**,
  **RAGAS faithfulness Δ**, and **context_precision** — with retrieval hit@k unchanged (compression is
  post-retrieval).
- **US-8 (Downstream F9 developer):** As the semantic-cache author, I want F8 to leave the retrieval
  seam, the `normalized` cache key, and the SSE contract unchanged, and to keep citation metadata on
  every (possibly trimmed) chunk, so the cache stores a correctly-cited compressed answer.

---

## 3. EARS acceptance criteria

### 3.1 Relevance floor
- **AC-1 (Ubiquitous — floor):** When compression is on, the system shall drop every chunk whose
  calibrated `rerank_score` is `< settings.COMPRESSION_SCORE_FLOOR` (default `0.25`).
- **AC-2 (Ubiquitous — min-chunks guarantee):** The floor shall never reduce a non-refused query's set
  below `settings.COMPRESSION_MIN_CHUNKS` (default `2`): if fewer than `MIN_CHUNKS` chunks clear the
  floor, the highest-scored chunks are retained up to `MIN_CHUNKS` (or the whole input when it already
  has ≤ `MIN_CHUNKS`).
- **AC-3 (Unwanted — no rerank score):** When a chunk's `rerank_score` is `None` (rerank off/absent),
  the floor shall not drop it (there is no calibrated signal to floor against); dedupe and the token
  budget still apply, so compression degrades gracefully without F6.

### 3.2 Dedupe (overlapping fixed chunks)
- **AC-4 (Ubiquitous — 5-gram Jaccard):** The system shall compute pairwise word-level 5-gram Jaccard
  similarity (`settings.COMPRESSION_DEDUPE_NGRAM = 5`) and, for any pair with similarity
  `> settings.COMPRESSION_DEDUPE_JACCARD` (default `0.7`), drop the **lower-`rerank_score`** chunk
  (ties broken by later rerank position), preserving the higher-scored one.
- **AC-5 (Ubiquitous — short-text safety):** A chunk with fewer than `NGRAM` words shall be compared by
  its full word-set (no crash on a too-short chunk), and dedupe shall never drop a chunk below the
  `MIN_CHUNKS` floor.

### 3.3 Token budget + sentence-level trimming
- **AC-6 (Ubiquitous — greedy fill):** The system shall count each kept chunk's tokens with tiktoken
  `cl100k_base` and greedily add chunks in **rerank order** until the running total would exceed
  `settings.COMPRESSION_TOKEN_BUDGET` (default `2200`).
- **AC-7 (Ubiquitous — overflow chunk trimmed, not dropped):** The single chunk that first overflows
  the budget shall be **sentence-trimmed** to the remaining budget rather than dropped whole; chunks
  after it are dropped. The top `COMPRESSION_MIN_CHUNKS` chunks are never dropped by the budget step
  (trimmed to fit if necessary), so the AC-2 floor survives the budget.
- **AC-8 (Ubiquitous — sentence scoring via the F6 cross-encoder):** Sentence trimming shall split the
  overflow chunk into sentences, score each `(query, sentence)` pair with the **same** cross-encoder
  F6 loaded (`rerank.get_rerank_model`), in **one batched** call executed **off the event loop**
  (`anyio.to_thread.run_sync`), and keep the highest-scored sentences that fit the remaining budget,
  re-emitted in their **original document order** (never reordered).
- **AC-9 (Ubiquitous — scoring query):** Sentence scoring and (implicitly) the whole compression step
  shall use the **F7 `normalized` query** when a rewrite result is present, else the raw query — the
  same query F6 reranked against.

### 3.4 Citation & metadata preservation
- **AC-10 (Ubiquitous — metadata preserved):** A trimmed chunk shall retain `chunk_id`, `doc_id`,
  `title`, `section_heading`, `page_start`, `page_end`, `anchor`, and all existing scores unchanged;
  only `text` is replaced (with a subset of its original sentences, verbatim). `extract_quote` shall
  therefore still produce a verbatim ≤25-word quote and the page mapping shall be intact.
- **AC-11 (Ubiquitous — numbering consistency):** The compressed chunk list shall be the single list
  handed to **both** `format_context` (numbered `[n]`) and `parse_citations`, so `[n]` markers map 1:1
  onto the compressed set and no citation points at a dropped chunk.

### 3.5 Metrics, fallback, cost
- **AC-12 (Ubiquitous — metrics logged):** Per request the system shall log `rag.compression` with
  `tokens_before`, `tokens_after`, `chunks_before`, `chunks_after`, `chunks_dropped`,
  `sentences_dropped`, and `compression_ms` via the central observability path (structlog now; routed
  to `request_logs`/Langfuse when F13 wires the central logger).
- **AC-13 (Unwanted — failure never blocks):** If any exception is raised in the compression path
  (scoring, splitting, trimming), the system shall log `compression_failed`, fall back to the
  **uncompressed** chunk list, and still answer (US-6).
- **AC-14 (Ubiquitous — no new OpenAI call / cost via existing path):** Compression shall add **no**
  OpenAI call; the token-cost win shall surface through the existing
  `observability.log_llm_cost(settings.LLM_MODEL, tokens_in, tokens_out)` in `_pipeline_events`, whose
  `tokens_in` already reflects the compressed context.

### 3.6 Async mandate
- **AC-15 (Ubiquitous — off-loop CPU):** The cross-encoder sentence scoring shall run off the loop via
  `anyio.to_thread.run_sync` (reusing F6's offload); tiktoken counting, n-gram/Jaccard math, dedupe,
  and greedy fill run **inline** as cheap pure-CPU (the CLAUDE.md-permitted side). **No** sync twin
  (`invoke`, `embed_query`, blocking `requests`, sync `redis`) shall appear in `compression.py` — the
  `app/rag/` async grep-guard (in `tests/rag/test_generation.py`) covers the new module.

### 3.7 Toggling, contracts, scope
- **AC-16 (State-driven — prod/request toggle):** While `ENABLE_COMPRESSION` is `false` (default),
  `_pipeline_events` shall skip compression entirely and behave **byte-for-byte** as
  `f7-rewrite-after` (same chunks, same prompt, same citations); while `true`, it shall run
  dedupe → floor → budgeted fill/trim before generation.
- **AC-17 (Ubiquitous — request/eval flag):** The system shall map `PipelineFlags.compression` onto
  `ENABLE_COMPRESSION` via the **same** `rag.flags.apply_flags` overlay (extended by one key).
  `PipelineFlags.compression` and `evals.flags.parse_flags` already accept the key, so `--flags
  compression=on` needs no parser change. `cache` stays force-`False` in the harness (unchanged).
- **AC-18 (Ubiquitous — SSE unchanged / no new stage):** Compression shall add **no** SSE stage
  (internal to the pipeline between the `searching` stage and generation); the ordered `stage* →
  token* → citations → meta → done|error` contract is unchanged, so F14/F17 need no contract change.
- **AC-19 (Ubiquitous — Settings):** Every new configuration value (`ENABLE_COMPRESSION`,
  `COMPRESSION_SCORE_FLOOR`, `COMPRESSION_MIN_CHUNKS`, `COMPRESSION_TOKEN_BUDGET`,
  `COMPRESSION_DEDUPE_JACCARD`, `COMPRESSION_DEDUPE_NGRAM`) shall live in the central
  `app.core.settings.Settings` class; `RERANK_MODEL` / `RERANK_DEVICE` / `RERANK_APPLY_SIGMOID` are
  reused for sentence scoring, not redefined.
- **AC-20 (Ubiquitous — no migration):** F8 shall add **no** Alembic migration — `RetrievedChunk` gains
  no field (trim mutates `text` on a copy), `AnswerResponse` gains no field, `PipelineFlags.compression`
  already exists. `alembic` autogenerate shall be empty.
- **AC-21 (Ubiquitous — eval gate):** The definition of done shall include running the F4 `--suite all`
  harness with `--flags hybrid=on,rerank=on,query_rewrite=on,compression=on --label
  f8-compression-after`, then `--compare f7-rewrite-after`, and committing the resulting
  `docs/eval_results/f8-compression-after.md` and
  `docs/eval_results/f8-compression-after-vs-f7-rewrite-after.md` delta reports, reporting the
  **prompt-token reduction** (target ≥25%), **RAGAS faithfulness Δ** (target ≤0.02 drop), and
  **context_precision**.

---

## 4. Acceptance criteria (feature-level definition of done)

1. **Relevance floor** unit-tested: chunks below `COMPRESSION_SCORE_FLOOR` are dropped; a set where
   fewer than `MIN_CHUNKS` clear the floor still returns `MIN_CHUNKS` highest-scored chunks; a chunk
   with `rerank_score=None` is never floored (AC-1/AC-2/AC-3).
2. **Dedupe** unit-tested: two chunks with 5-gram Jaccard `> 0.7` collapse to the higher-`rerank_score`
   one; a below-threshold pair is untouched; a <5-word chunk compares safely; dedupe never drops below
   `MIN_CHUNKS` (AC-4/AC-5).
3. **Token budget + trim** unit-tested (mocked cross-encoder): chunks greedily fill to
   `COMPRESSION_TOKEN_BUDGET`; the overflow chunk is sentence-trimmed to fit (kept sentences are the
   top-scored, re-emitted in original order); chunks after it are dropped; the top `MIN_CHUNKS` are
   never dropped; the sentence scoring is one batched off-loop call against the scoring query
   (AC-6/AC-7/AC-8/AC-9).
4. **Citation safety** unit-tested: a trimmed chunk keeps every metadata field and existing scores,
   only `text` shrinks, and `extract_quote` still yields a verbatim ≤25-word quote with intact page
   mapping (AC-10); the compressed list drives both `format_context` and `parse_citations` so `[n]`
   maps 1:1 (AC-11).
5. **Fallback** unit-tested: an exception in the scoring/trim path yields the uncompressed chunks, a
   logged `compression_failed`, and a completed answer (AC-13).
6. **Metrics** unit-tested: one `rag.compression` record per compressed request carrying
   `tokens_before`/`tokens_after`/`chunks_dropped`/`sentences_dropped`/`compression_ms` (AC-12); no new
   `estimate_cost` site is added for compression (AC-14).
7. **Toggle parity** asserted: `ENABLE_COMPRESSION=false` makes `_pipeline_events` byte-for-byte
   `f7-rewrite-after` (same chunks/prompt/citations) for a fixed mocked pool (AC-16);
   `apply_flags(PipelineFlags(compression=True)).ENABLE_COMPRESSION is True` and the input settings are
   unmutated (AC-17); `parse_flags("…,compression=on").compression is True` with `cache` still `False`.
8. **Contracts** asserted: no SSE stage added and the ordered event contract still holds end-to-end
   (AC-18); `alembic` autogenerate is empty (AC-20); the `app/rag/` async grep-guard covers
   `compression.py` and is green (AC-15).
9. **LangChain API surface** unit-tested: `build_document_compressor(settings)` returns a
   `DocumentCompressorPipeline` stacking the F6 `CrossEncoderReranker` with the F8 filters over the
   same loaded model, and is never referenced on the request path.
10. **Eval gate:** `docs/eval_results/f8-compression-after.md` and
    `docs/eval_results/f8-compression-after-vs-f7-rewrite-after.md` are committed; the delta shows
    **prompt-token reduction ≥25%** (else thresholds are tuned and the deviation documented),
    **RAGAS faithfulness drop ≤0.02**, **context_precision reported**, and **retrieval hit@k
    unchanged** vs `f7-rewrite-after` (AC-21).
11. Every AC above is covered by an automated test — this list is the test list, not aspiration.

---

## 5. Out of scope (do not implement here)

- **Semantic cache (F9), session memory (F17):** F8 leaves the `normalized` cache key and the SSE
  contract intact for F9 and does not build the cache or the memory store; memory is only *consumed*
  through the existing `_pipeline_events` param (for the scoring query), never built here.
- **A new SSE `compressing` stage / `app/memory/stages.py` emitter:** F8 adds no stage (AC-18);
  surfacing a dedicated compression stage is F17/F14's job via the emitter they own.
- **Re-embedding / re-ranking / changing chunk boundaries:** compression is a pre-generation transform
  over the F6 reranked top-5; it forces **no** re-index/re-embed and does not alter F2 chunking, so
  `f7-rewrite-after` retrieval numbers stay comparable.
- **An LLM-based or embedding-similarity compressor (`LLMChainExtractor`, `EmbeddingsFilter`):** F8's
  relevance/sentence signal is the **already-loaded** cross-encoder (no new model call); an LLM
  extractor would add exactly the cost F8 is cutting. Alternative compressors are later tuning, not
  this gate.
- **Persisting compression metrics to `request_logs`:** the central request-log writer is F13; F8 logs
  `rag.compression` via structlog (the F3/F5/F6/F7 convention).
- **Compressing memory / history tokens:** F8 compresses only the retrieved context block; the F17
  memory budget (sliding window + rolling summary) is a separate mechanism owned by F17.
