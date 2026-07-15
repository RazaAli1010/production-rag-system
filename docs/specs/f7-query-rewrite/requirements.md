# F7 — Query Rewriting (Normalization + Multi-Query) · requirements.md

**Module:** `backend/app/rag/rewrite.py` (+ a rewrite step wrapping the `retriever.retrieve` seam)
**Phase:** B (retrieval enhancement #3) · **Depends on:** F6 (rerank + `RetrievedChunk` scores + the
fused candidate pool), F5 (hybrid pool), F4 (eval harness) · **Flag:** `ENABLE_QUERY_REWRITE`
**Model:** the rewrite LLM call uses **`gpt-4o-mini`** (`settings.REWRITE_MODEL`, default
`"gpt-4o-mini"`) — the project-wide primary model; `gpt-4o` deep mode is **not** used here.
**Eval gate:** `f6-rerank-after` → **`f7-rewrite-after`** (third `--compare` gate in the fixed
sequence).

---

## 1. Overview

F5/F6 improved *how* a query is retrieved and reranked, but they still feed the retriever the
student's **raw** text. Our users are Pakistani students on mobile typing messy, typo-ridden,
Urdu/English code-switched questions ("cgpa prob se kesay niklun", "aur MPhil ka?"). BM25 tokens and
dense embeddings both degrade on this input: the lexical index has no token overlap with English
regulation text, and the embedding of a half-transliterated fragment lands far from the clause that
answers it. F7 inserts **one `gpt-4o-mini` rewrite call** *before* retrieval that:

1. **Normalizes** — fixes typos, expands abbreviations, and translates the code-switched fragment
   into a clean, searchable **English** question
   ("cgpa prob se kesay niklun" → "how to get off academic probation CGPA requirements").
2. **Condenses (history-aware, F17-ready)** — when a `MemoryContext` is present, resolves
   pronouns/ellipsis in a follow-up into a **standalone** question ("aur MPhil ka?" after a
   BS-deadline turn → "What is the MPhil admission deadline?"). The standalone `normalized` query is
   what retrieval **and** the future F9 semantic-cache key both consume, so follow-ups never poison
   the cache.
3. **Expands (multi-query)** — emits **2 paraphrase variants** emphasizing different terms. Hybrid
   retrieval (F5) fans out over `normalized` + the 2 variants; the per-query pools are **union +
   RRF-merged** into one candidate pool that feeds a **single** F6 rerank.
4. **Declares answer language** — returns `language ∈ {"en","ur-mix"}`, passed **explicitly** to the
   generation prompt (not inferred by the model), so the answer register matches the question.

F7 is a **pre-retrieval transform**, not a change to the retrieval algorithm: `hybrid_retrieve`
(F5), `rrf_fuse`, `rerank_chunks` (F6), the prompt/citation/SSE contracts, and the `bm25.pkl` / dense
index are all untouched. F7 lives in a new module `app/rag/rewrite.py` and wraps the existing
`retriever.retrieve` seam behind the `ENABLE_QUERY_REWRITE` flag: **flag off (default) delegates
verbatim to `retriever.retrieve`, i.e. byte-for-byte the `f6-rerank-after` path**; flag on runs
rewrite → fan-out → union+RRF-merge → single rerank. The F4 harness drives the toggle via
`--flags query_rewrite=on/off`.

Because rewrite adds **+1 `gpt-4o-mini` call per uncached request**, the cost/query and latency
deltas are part of the gate. Rewrite failure (timeout, bad JSON, provider error) **never blocks
answering** — the pipeline falls back to the raw query and logs `rewrite_failed`.

### 1.1 Design decisions resolved in the feature brief (do NOT re-derive)

- **Custom fan-out is the runtime path (no LangChain `MultiQueryRetriever`).**
  `MultiQueryRetriever` generates variants and unions the results but **discards per-query and
  per-stage scores** and does not RRF-merge, so it cannot feed F6's score-driven rerank or the
  calibrated refusal gate — exactly the reason F6 rejected `CrossEncoderReranker.compress_documents`
  on the hot path. The runtime path is therefore our own fan-out + `rrf_merge` + `rerank_chunks`; the
  LangChain `MultiQueryRetriever` is **not** built (no off-path API-surface deliverable in F7).
- **Rewrite decomposes into three callables so F9 is not painted into a corner.**
  `rewrite_query` (the LLM call → `RewriteResult`), `multi_query_retrieve` (fan-out + merge + single
  rerank over an *already-computed* `RewriteResult`), and a convenience `retrieve` wrapper that
  chains them. Now (pre-F9) `_pipeline_events` and the F4 retrieval suite call the wrapper; when F9
  lands, `_pipeline_events` will call `rewrite_query` first (so the **cache lookup keys on the
  normalized standalone query** before retrieval), then `multi_query_retrieve` on a cache miss — **no
  double rewrite**.
- **Memory flows through the existing `_pipeline_events` `memory` param**, not new plumbing.
  `_pipeline_events` already receives `memory: MemoryContext | None`; F7 threads it into
  `rewrite.retrieve`, so condensation activates automatically once F17 populates it. The F4 harness
  and the retrieval suite pass `memory=None`, so at the gate rewrite is pure normalization +
  multi-query (no condensation) — which is precisely the retrieval behaviour the gate measures.
- **The rewrite result is surfaced out-of-band via a ContextVar** (`last_rewrite()`), mirroring F5's
  `_DEGRADED` / `was_degraded()` pattern, so the retrieval seam's return shape
  (`-> list[RetrievedChunk]`) is unchanged and `_pipeline_events` still gets `language` (for the
  prompt) and `normalized` (for the future F9 key).
- **No new SSE stage** (mirrors F5/F6): rewrite is internal to the existing `searching` stage. The
  ordered `stage* → token* → citations → meta → done|error` contract is unchanged, so F14/F17 need no
  contract change; F17 (which owns the `app/memory/stages.py` emitter) may later surface a dedicated
  `rewriting` stage without touching F7.

---

## 2. User stories

- **US-1 (Student — code-switch translation):** As a student typing "cgpa prob se kesay niklun", I
  want the system to understand and search as if I asked "how to get off academic probation CGPA
  requirements", so I get the right clause instead of a refusal driven by zero token overlap.
- **US-2 (Student — typo / abbreviation tolerance):** As a student who writes "hec plag policy" or
  "reevaluation fee", I want typos fixed and abbreviations expanded before retrieval, so messy input
  still finds the correct document.
- **US-3 (Student — coherent follow-up):** As a student asking "aur MPhil ka?" right after a
  BS-deadline answer, I want the system to resolve my ellipsis into a standalone question and answer
  about the MPhil deadline — not re-answer BS — so a conversation feels coherent (activates with
  F17; the mechanism ships in F7).
- **US-4 (Student — recall via paraphrase):** As a student whose phrasing differs from the
  regulation's wording, I want the system to also search 2 paraphrases of my question so a passage
  that uses different terms than I did is still retrieved.
- **US-5 (Ops / cost owner):** As the person paying the bill, I want rewrite behind one flag
  (`ENABLE_QUERY_REWRITE`) with the F6 path preserved verbatim when off, so I can A/B the +1
  `gpt-4o-mini` call against its retrieval lift and roll back instantly in prod.
- **US-6 (Reliability owner):** As the on-call, I want a rewrite timeout / bad-JSON / provider error
  to **fall back to the raw query and still answer** (logged `rewrite_failed`), never a blocked or
  errored response, so a flaky rewrite call cannot take down Q&A.
- **US-7 (Eval author):** As the person running the gate, I want to A/B `f6` vs `f6+rewrite` under
  the F4 harness with a **single backward-compatible** retrieval-seam swap, so `code_switched` hit@5
  (the headline), `en` no-regression, cost/query and p95 deltas are directly comparable to
  `f6-rerank-after`.
- **US-8 (Downstream F8/F9 developer):** As the compression / cache author, I want F7 to leave the
  seam return type and SSE contract unchanged, expose the standalone `normalized` query
  (`last_rewrite()`) as the future cache key, and keep `RetrievedChunk` scores populated, so F8/F9
  consume the reranked top-5 and the normalized key without touching rewrite code.

---

## 3. EARS acceptance criteria

### 3.1 The rewrite call (one `gpt-4o-mini` call, JSON, hardened)
- **AC-1 (Ubiquitous — model & params):** The system shall perform **exactly one** rewrite LLM call
  per uncached request via `gpt-4o-mini` (`settings.REWRITE_MODEL`, default `"gpt-4o-mini"`) at
  `temperature = settings.REWRITE_TEMPERATURE` (default `0.0`), `max_tokens =
  settings.REWRITE_MAX_TOKENS` (default `200`), in **JSON output mode**, over the async surface
  (`ainvoke`) — never a sync `invoke`.
- **AC-2 (Ubiquitous — output contract):** The call shall return a `RewriteResult` with
  `normalized: str`, `variants: list[str]` (length `settings.REWRITE_NUM_VARIANTS`, default `2`), and
  `language: Literal["en","ur-mix"]`. `normalized` shall be cleaned, translated to English, and
  abbreviation-expanded; `variants` shall be 2 paraphrases emphasizing different terms.
- **AC-3 (Event-driven — history-aware condensation):** When a non-empty `MemoryContext` is supplied,
  the rewrite prompt shall receive the rendered memory and resolve pronouns/ellipsis into a
  **standalone** question (US-3). When `memory is None`/empty (Phase B, the F4 harness, pre-F17),
  the rewrite shall perform normalization + multi-query only, with no condensation.
- **AC-4 (Ubiquitous — injection hardening):** The rewrite prompt shall treat the query text as
  **data, not instructions** (explicit hardening line) and rely on JSON output mode, so an injection
  attempt inside the query cannot change the rewrite's behaviour or escape the JSON envelope.

### 3.2 Fan-out, merge, single rerank
- **AC-5 (Ubiquitous — fan-out set):** The system shall build the retrieval query set as
  `dedupe([normalized, *variants])` and run **hybrid retrieval (F5)** for each, bounded by
  `asyncio.Semaphore(settings.REWRITE_FANOUT_CONCURRENCY)` (default `3`) via `asyncio.gather`.
- **AC-6 (Ubiquitous — union + RRF-merge):** The per-query candidate pools shall be **union +
  RRF-merged** by `chunk_id` (`Σ 1/(REWRITE_RRF_K + rank)` across the lists a chunk appears in,
  default `REWRITE_RRF_K = 60`), deduped, sorted by merged score, and capped at
  `settings.REWRITE_MERGED_TOP_K` (default `12`, matching `RERANK_CANDIDATE_K`) — **whole
  `RetrievedChunk` objects** carried through the merge (no parallel-array re-zip).
- **AC-7 (Ubiquitous — single rerank):** When `ENABLE_RERANK` is on, the merged pool shall be
  reranked by **one** `rerank.rerank_chunks(normalized, merged_pool, settings)` call against the
  **normalized** query, returning `RERANK_TOP_N` (5); when `ENABLE_RERANK` is off, the merged pool is
  truncated to `k`. The count handed to generation stays `k` (=5).
- **AC-8 (Ubiquitous — latency budget):** The rewrite step shall add **≤ 600 ms p50** end-to-end;
  `rewrite_ms` shall be recorded. A `settings.REWRITE_TIMEOUT_S` (default `5.0`) bounds the rewrite
  call so it can never hang the answer.

### 3.3 Answer language (explicit)
- **AC-9 (Ubiquitous — explicit language):** The `RewriteResult.language` shall be passed explicitly
  into the generation prompt as a rendered directive (`prompt.render_language_directive`), not
  inferred by the model. When rewrite is off/failed, the directive is empty and the existing
  "respond in the question's language" prompt rule stands (no regression).

### 3.4 Fallback & cost
- **AC-10 (Unwanted — rewrite failure never blocks):** If the rewrite call times out, returns
  non-JSON / schema-invalid output, or raises, the system shall fall back to a `RewriteResult` with
  `normalized = raw_query`, `variants = []`, `language = None`, mark it `failed = True`, log
  `rewrite_failed`, and **proceed to answer** with the raw single query (retrieval never blocked,
  US-6).
- **AC-11 (Ubiquitous — cost logged):** The rewrite call's token usage and estimated cost shall be
  logged via the central `observability.log_llm_cost(settings.REWRITE_MODEL, tokens_in, tokens_out)`
  (`gpt-4o-mini` pricing through the shared `estimate_cost`), so the cost/query delta is measurable
  at the gate (FR5).

### 3.5 Edge cases
- **AC-12 (Ubiquitous — already-clean English near-identity):** For an already-clean English query,
  `normalized` shall be a near-identity rewrite (the prompt instructs "if the question is already
  clean English, return it essentially unchanged"), so the `en` slice does not regress (gate: `en`
  hit@5 must not drop > 1 point).
- **AC-13 (Ubiquitous — section-number preservation):** For a section-number-only query
  ("regulation 15(3)?"), at least one member of `[normalized, *variants]` shall preserve the exact
  section tokens (`15(3)`) verbatim, so the exact-identifier retrieval path is not lost to
  paraphrasing.
- **AC-14 (Unwanted — degenerate rewrite output):** Empty/whitespace `normalized`, missing
  `variants`, or a bad `language` value shall be coerced to safe defaults (fall back `normalized` to
  the raw query; drop empty variants; `language=None` if not one of `{"en","ur-mix"}`) rather than
  propagating a broken pool or an unhandled error.

### 3.6 Toggling, contracts, observability & scope
- **AC-15 (State-driven — prod/request toggle):** While `ENABLE_QUERY_REWRITE` is `false` (default),
  `rewrite.retrieve` shall delegate to `retriever.retrieve` and behave **byte-for-byte** as
  `f6-rerank-after` (no rewrite call, same chunks/order/refusal); while `true`, it shall run the
  rewrite + fan-out + merge + single-rerank path.
- **AC-16 (Ubiquitous — request/eval flag):** The system shall map `PipelineFlags.query_rewrite` onto
  `ENABLE_QUERY_REWRITE` via the **same** `rag.flags.apply_flags` overlay (extended by one key),
  applied at the same two seams (`baseline._pipeline_events`, `evals.retrieval.run_retrieval`);
  `PipelineFlags.query_rewrite` and `parse_flags` already accept the key (no new wiring there).
- **AC-17 (Ubiquitous — seam swap is backward-compatible & SSE unchanged):** `_pipeline_events` and
  the F4 retrieval suite shall call `rewrite.retrieve(query, k, namespace, settings, memory)` in
  place of `retriever.retrieve(...)`; because `rewrite.retrieve` delegates verbatim when the flag is
  off, `baseline`/`f5-hybrid-after`/`f6-rerank-after` remain byte-for-byte unchanged. **No** new SSE
  stage is added (rewrite is internal to `searching`), and prompt/`format_context`/`parse_citations`
  are untouched except for the additive `{language_directive}` slot.
- **AC-18 (Ubiquitous — out-of-band result):** The system shall expose the `RewriteResult` via
  `rewrite.last_rewrite()` (read-and-reset ContextVar, mirroring `hybrid.was_degraded()`), so
  `_pipeline_events` obtains `language` and the standalone `normalized` query without changing the
  retrieval seam's `-> list[RetrievedChunk]` return type.
- **AC-19 (Ubiquitous — metrics logged):** The system shall log `rewrite_ms`, `n_variants`,
  `n_fanout_queries`, `language`, `rewrite_failed`, and the merged/reranked candidate count via the
  central observability path (structlog now; flows to `request_logs`/Langfuse when F13 wires the
  central logger), plus the LLM cost (AC-11).
- **AC-20 (Ubiquitous — Settings):** Every new configuration value (`ENABLE_QUERY_REWRITE`,
  `REWRITE_MODEL`, `REWRITE_TEMPERATURE`, `REWRITE_MAX_TOKENS`, `REWRITE_NUM_VARIANTS`,
  `REWRITE_RRF_K`, `REWRITE_MERGED_TOP_K`, `REWRITE_FANOUT_CONCURRENCY`, `REWRITE_TIMEOUT_S`) shall
  live in the central `app.core.settings.Settings` class; `RERANK_TOP_N` / `RERANK_CANDIDATE_K` /
  `HYBRID_*` are reused, not redefined. `REWRITE_MODEL` defaults to **`"gpt-4o-mini"`**.
- **AC-21 (Ubiquitous — async mandate):** The rewrite path shall be async end-to-end: the LLM call is
  `ainvoke`, the fan-out is `asyncio.gather` bounded by a `Semaphore`, JSON parsing and RRF-merge math
  run inline as cheap pure-CPU, and the single rerank reuses F6's off-loop `score` offload. **No** sync
  twin (`invoke`, `embed_query`, blocking `requests`, sync `redis`) shall appear in `rewrite.py`.
- **AC-22 (Ubiquitous — no migration):** F7 shall add **no** Alembic migration — `RewriteResult` is
  transient (never persisted), `AnswerResponse` gains no field, `PipelineFlags.query_rewrite` already
  exists, and no table is added. `alembic` autogenerate shall be empty.
- **AC-23 (Ubiquitous — eval gate):** The definition of done shall include running the F4
  `--suite all` harness with `--flags hybrid=on,rerank=on,query_rewrite=on --label f7-rewrite-after`,
  then `--compare f6-rerank-after`, and committing the resulting
  `docs/eval_results/f7-rewrite-after-vs-f6-rerank-after.md` delta report.

---

## 4. Acceptance criteria (feature-level definition of done)

1. **Rewrite call** is unit-tested (mocked `gpt-4o-mini`): parses JSON into `RewriteResult`
   (`normalized`/`variants`/`language`), uses `settings.REWRITE_MODEL == "gpt-4o-mini"`,
   `temperature=0`, `max_tokens=200`, JSON mode, via `ainvoke` (AC-1/AC-2/AC-21).
2. **Condensation** is unit-tested with a `MemoryContext` fixture: "aur MPhil ka?" after a
   BS-deadline pair rewrites to a standalone MPhil question; with `memory=None` the same query is
   normalized without condensation (AC-3).
3. **Fan-out + union + RRF-merge** is unit-tested: 3 mocked per-query pools union and RRF-merge by
   `chunk_id`, whole objects survive (no re-zip drift), capped at `REWRITE_MERGED_TOP_K` (AC-5/AC-6).
4. **Single rerank** is unit-tested: with rerank on, exactly **one** `rerank_chunks` call on the
   merged pool against `normalized`, output length `RERANK_TOP_N`; with rerank off, merged pool
   truncated to `k` (AC-7).
5. **Language passthrough** is unit-tested: `language` renders an explicit directive into the prompt
   input; rewrite off/failed → empty directive, existing behaviour (AC-9).
6. **Fallback** is unit-tested: timeout / non-JSON / schema-invalid / raised rewrite → raw-query
   `RewriteResult(failed=True)`, `rewrite_failed` logged, pipeline still answers with the raw single
   query (AC-10/AC-14).
7. **Edge cases** unit-tested: already-clean English → near-identity `normalized` (AC-12);
   "regulation 15(3)?" → a query in the set preserves `15(3)` verbatim (AC-13); an injection string
   in the query does not alter the JSON contract (AC-4).
8. **Toggle parity** is asserted: `ENABLE_QUERY_REWRITE=false` makes `rewrite.retrieve` return
   exactly `retriever.retrieve`'s result (same chunks/order) — byte-for-byte `f6-rerank-after` — for a
   fixed mocked pool (AC-15/AC-17); `apply_flags` maps `flags.query_rewrite` and the retrieval suite
   re-measures through the wrapper (AC-16).
9. **Out-of-band result** unit-tested: `last_rewrite()` returns the `RewriteResult` after
   `rewrite.retrieve` and resets to `None` (AC-18).
10. **Cost & metrics** unit-tested: one `rag.llm_cost` (`gpt-4o-mini`) and one `rag.rewrite`
    (`rewrite_ms`/`n_variants`/`n_fanout_queries`/`language`/`rewrite_failed`) record per rewrite
    (AC-11/AC-19); async grep-guard covers `rewrite.py` (AC-21); `alembic` autogenerate empty (AC-22).
11. **Eval gate:** `docs/eval_results/f7-rewrite-after.md` and
    `docs/eval_results/f7-rewrite-after-vs-f6-rerank-after.md` are committed; the delta table shows
    the headline — **`code_switched` hit@5 up**, **`en` hit@5 not down > 1 point** — with cost/query
    and p95 deltas, overall and per slice (AC-23).
12. Every AC above is covered by an automated test — this list is the test list, not aspiration.

---

## 5. Out of scope (do not implement here)

- **Compression (F8), semantic cache (F9), session memory (F17):** F7 ships the rewrite mechanism
  and exposes the standalone `normalized` query for the F9 cache key, but does **not** build the
  cache, the compressor, or the memory store/summarizer. Memory is only *consumed* (threaded through
  the existing `_pipeline_events` param); F17 populates it.
- **A new SSE `rewriting` stage / `app/memory/stages.py` emitter:** F7 adds no stage (AC-17);
  surfacing a dedicated rewrite stage is F17/F14's job via the emitter they own.
- **Persisting the rewritten/normalized query to `request_logs`:** the central request-log writer is
  F13; F7 logs metrics via structlog (the F3/F5/F6 convention). Raw query text stays hashed-only in
  `request_logs` by the existing privacy rule — the normalized query is telemetry, not persisted here.
- **Changing F5 fusion, F6 rerank, `bm25.pkl`, or the dense index:** F7 is a pre-retrieval transform;
  it forces **no** re-index/re-embed, so `f6-rerank-after` numbers stay comparable.
- **A second rewrite model / model A-B / `gpt-4o` deep rewrite:** only `gpt-4o-mini` is wired for
  rewrite (AC-1); swapping models is later tuning, not this gate.
- **Wiring the F9 lookup-before-retrieve handoff:** F7 provides the `rewrite_query` /
  `multi_query_retrieve` decomposition so F9 can rewrite-then-lookup without a double rewrite, but the
  cache lookup itself is F9.
