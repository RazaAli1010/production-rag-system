# F5 — Hybrid Search (BM25 + Dense + RRF) · requirements.md

**Module:** `backend/app/rag/hybrid.py` (+ a body swap in `backend/app/rag/retriever.py`)
**Phase:** B (retrieval enhancement #1) · **Depends on:** F3 (baseline chain + the `retrieve` seam),
F4 (eval harness) · **Flag:** `ENABLE_HYBRID`
**Eval gate:** `baseline` → **`f5-hybrid-after`** (the first `--compare` gate in the fixed sequence).

---

## 1. Overview

F3's `retrieve(query, k, namespace, settings) -> list[RetrievedChunk]` is dense-only: it embeds the
query and pulls the top-`k` cosine neighbours from Pinecone. Dense retrieval misses **exact-term**
queries — a student asking about "Section 10.3", form "PU-EX-14", a fee code, or an Urdu keyword
that has no near-synonym in embedding space. F5 adds an in-process **BM25** sparse retriever over the
same corpus, runs dense and sparse in parallel, and fuses the two ranked lists with **Reciprocal
Rank Fusion (RRF)** so a document that either retriever ranks highly surfaces near the top.

F5 is the **body** of the F3→F5 seam, not a new seam: `app.rag.retriever.retrieve` keeps its exact
signature and return type. The prompt, context formatting, citation parsing, refusal, and SSE
contract are untouched — this is precisely the "swap the retrieval step without touching prompt,
parsing, or streaming" property F3's design (§5) reserved. F5's dense-only path is the `baseline`
label; its hybrid path is the `f5-hybrid-after` label; the delta between them is the eval gate.

F5 is fully toggleable: `ENABLE_HYBRID=false` (default) is byte-for-byte the F3 dense path, so prod
rollback and A/B are a config flip. The F4 harness's `--flags hybrid=on/off` drives the same toggle.

---

## 2. User stories

- **US-1 (Student — exact terms):** As a student asking "semester rules ka section 10.3 kya kehta
  hai", I want the exact clause retrieved even though the dense model treats "10.3" as noise, so I
  get the specific rule and not a vaguely-related paragraph.
- **US-2 (Student — code-switched keywords):** As a student typing Urdu/English keywords ("پروبیشن
  fee refund"), I want lexical matches on the literal tokens I used, so rare terms with no embedding
  neighbour still hit.
- **US-3 (Ops / cost owner):** As the person paying the bill, I want hybrid to be a single config
  flag (`ENABLE_HYBRID`) with the dense-only path preserved verbatim, so I can A/B it and roll back
  in prod instantly if fusion regresses any slice.
- **US-4 (Eval author):** As the person running the gate, I want to A/B `dense_only`, `bm25_only`,
  and `hybrid` under the F4 harness with **zero F4 code change**, so hit@k / MRR deltas are directly
  comparable to `baseline`.
- **US-5 (Reliability owner):** As the on-call, I want a Pinecone outage to degrade to BM25-only
  (flagged `degraded=true`) rather than fail the whole answer, so keyword search still serves users
  when the vector store is down.
- **US-6 (Downstream F6 developer):** As the reranking author, I want F5 to expose a **12-candidate**
  fused pool (with dense/sparse/fused scores populated on each `RetrievedChunk`), so I can insert a
  cross-encoder rerank step between fusion and generation without changing the fusion code.
- **US-7 (Corpus integrity):** As the person who owns the BM25 index, I want the query tokenizer to
  be the **exact same** `urdu_safe_tokenize` used to build `bm25.pkl`, so Urdu tokens are not
  destroyed and query/corpus tokenization can never drift apart.

---

## 3. EARS acceptance criteria

### 3.1 Sparse (BM25) retrieval
- **AC-1 (Ubiquitous):** The system shall load the BM25 index and its `chunk_id` order from
  `settings.BM25_PATH` (`bm25.pkl`) exactly once, via `anyio.to_thread.run_sync` (blocking pickle
  read is CPU/IO-bound, off the loop per the CLAUDE.md async mandate), and cache it in-process.
- **AC-2 (Unwanted — missing index):** If `bm25.pkl` is absent or unreadable when hybrid mode is
  first used, then the system shall raise a clear, typed startup error naming the missing path (fail
  fast) rather than silently degrading to dense-only.
- **AC-3 (Ubiquitous):** The system shall tokenize the query with the **same** `urdu_safe_tokenize`
  function used by `app.indexing.bm25` at build time, preserving Urdu-range tokens, and score the
  corpus with `BM25Okapi.get_scores`, returning the top `HYBRID_SPARSE_TOP_K` (default 20)
  `(chunk_id, sparse_score, rank)` triples. BM25 scoring runs **inline** (pure-CPU numpy over the
  small ~600-chunk corpus, the same side of the line as the cache-matrix cosine matmul the mandate
  permits inline) — stated explicitly.
- **AC-4 (Ubiquitous):** The system shall hydrate sparse-only `chunk_id`s (those not already present
  in the dense result) into full `RetrievedChunk`s via an async Pinecone `fetch` by id against the
  requested namespace(s), reusing the F2 metadata already stored on each vector — no Postgres session
  is added to the `retrieve` seam signature, and no extra embedding call is made.

### 3.2 Dense retrieval (raised k)
- **AC-5 (Ubiquitous):** The system shall retrieve the dense top `HYBRID_DENSE_TOP_K` (default 20,
  raised from F3's 5) via the existing `PineconeVectorStore.asimilarity_search_with_score` path,
  honouring the F3 namespace fan-out (`namespace=None` → concurrent `pu`+`hec`, merged) unchanged.

### 3.3 Reciprocal Rank Fusion
- **AC-6 (Ubiquitous):** The system shall dedupe candidates by `chunk_id` **before** fusion, so a
  chunk appearing in both the dense and sparse lists is a single fused entry carrying both its dense
  and sparse rank.
- **AC-7 (Ubiquitous):** The system shall compute, for each distinct candidate, an RRF score
  `Σ 1/(HYBRID_RRF_K + rank_i)` (default `HYBRID_RRF_K=60`) summed over the lists it appears in
  (`rank_i` 1-indexed); a list in which the candidate does not appear contributes `0`.
- **AC-8 (Ubiquitous):** The system shall populate `dense_score`, `sparse_score`, and `fused_score`
  on every returned `RetrievedChunk` (`None` on a per-stage score the chunk did not receive, e.g.
  `dense_score=None` for a sparse-only hit), and order the fused pool by `fused_score` descending.
- **AC-9 (Ubiquitous):** The system shall produce a fused **candidate pool of up to
  `HYBRID_FUSED_TOP_K` (default 12)** internally, and the `retrieve` seam shall return the top `k`
  of that pool (default `k=5`), so the count handed to generation stays 5 until F6 inserts reranking
  (F5 changes the retrieval quality at fixed generation-`k`, not the generation-`k` itself).
- **AC-10 (Ubiquitous):** The system shall implement RRF as a **custom** fusion (not LangChain
  `EnsembleRetriever`), justified because per-stage `dense_score`/`sparse_score`/`fused_score` and
  per-stage ranks must be exposed on `RetrievedChunk` for F6 and the eval report — a capability
  `EnsembleRetriever` (which returns fused `Document`s with no per-stage provenance) does not offer.

### 3.4 Toggling & eval modes
- **AC-11 (State-driven — prod/request toggle):** While `ENABLE_HYBRID` is `false` (default), the
  system shall behave byte-for-byte as the F3 dense-only path (same code path, same numbers as
  `baseline`); while `true`, it shall use the hybrid fusion path.
- **AC-12 (Ubiquitous — request flag):** The system shall map the request/eval `PipelineFlags.hybrid`
  onto the effective retrieval mode via a single shared overlay helper applied at both call sites
  (the F3 pipeline generator and the F4 retrieval suite), so the CLAUDE.md "toggleable via a
  config/request flag" rule holds and the F4 comment's reserved "retrieve() reads toggles from
  settings (F5+)" contract is fulfilled without changing how any suite *measures*.
- **AC-13 (Ubiquitous — eval A/B modes):** The system shall expose three measurable retrieval modes —
  `dense_only`, `bm25_only`, `hybrid` — selectable for evaluation via `RETRIEVAL_MODE` (an
  eval-only explicit override that wins over `ENABLE_HYBRID`), so each can be scored under the F4
  harness against `baseline` with no F4 code change.

### 3.5 Degraded mode & refusal interaction
- **AC-14 (Event-driven — Pinecone failure):** When the dense (Pinecone) retrieval fails or times out
  while hybrid is enabled, the system shall fall back to **BM25-only** results, record the fallback
  (structlog warning + `AnswerResponse.degraded=true`), and still return an answer rather than
  raising — BM25 does not depend on Pinecone.
- **AC-15 (State-driven — fusion vs refusal gate):** While hybrid is enabled, the pre-LLM refusal
  gate shall evaluate the **maximum `dense_score` across the fused set** (ignoring `None`s), not
  `chunks[0].dense_score`, so a strong sparse-only hit landing at fused position 0 does not
  spuriously trigger a `low_retrieval_confidence` refusal on an in-corpus query — while a query with
  no dense support anywhere above `REFUSAL_DENSE_THRESHOLD` is still refused (out-of-corpus
  protection preserved, "refusal not hallucination" intact).

### 3.6 Contracts, observability & scope
- **AC-16 (Ubiquitous):** The system shall keep the F3→F5 seam signature identical
  (`retrieve(query, k, namespace, settings) -> list[RetrievedChunk]`); prompt, `format_context`,
  `parse_citations`, the SSE contract, and every stage name are unchanged.
- **AC-17 (Ubiquitous):** The system shall add `degraded: bool = False` to `AnswerResponse` (an
  additive, non-persisted Pydantic contract field — no Alembic migration), defaulting `false` so F3's
  existing behaviour and all prior tests are unaffected.
- **AC-18 (Ubiquitous):** Every new configuration value (`ENABLE_HYBRID`, `RETRIEVAL_MODE`,
  `HYBRID_DENSE_TOP_K`, `HYBRID_SPARSE_TOP_K`, `HYBRID_FUSED_TOP_K`, `HYBRID_RRF_K`) shall live in the
  central `app.core.settings.Settings` class; `BM25_PATH` is reused verbatim from F2.
- **AC-19 (Ubiquitous):** The system shall keep the whole hybrid path async end-to-end: dense query,
  namespace fan-out, and sparse-hit `fetch` are awaited; the only thread-offload is the one-time
  `bm25.pkl` load (AC-1); tokenize / `get_scores` / RRF math run inline as declared cheap pure-CPU.
- **AC-20 (Ubiquitous — eval gate):** The system's definition of done shall include running the F4
  `--suite all` harness with `--flags hybrid=on --label f5-hybrid-after`, then
  `--compare baseline`, and committing the resulting `docs/eval_results/f5-hybrid-after-vs-baseline.md`
  delta report.

---

## 4. Acceptance criteria (feature-level definition of done)

1. **RRF math** is unit-tested on synthetic ranked lists: known dense/sparse rankings produce the
   hand-computed `fused_score` ordering; a chunk in both lists outranks equally-ranked single-list
   chunks (AC-6/AC-7).
2. **Dedupe** is unit-tested: a `chunk_id` present in both lists yields exactly one fused entry
   carrying both ranks/scores (AC-6/AC-8).
3. **Degraded mode** is unit-tested: a mocked Pinecone failure yields BM25-only results with
   `degraded=true` and no raise (AC-14).
4. **Refusal interaction** is unit-tested: a sparse-only top-ranked chunk (dense_score `None`) with a
   supporting dense chunk above threshold deeper in the pool does **not** refuse; a fused set whose
   every dense_score is below threshold **does** refuse (AC-15).
5. **Urdu tokenizer parity** is unit-tested: a code-switched query tokenizes via the same
   `urdu_safe_tokenize` as the corpus builder and preserves Urdu-range tokens (AC-3/US-7).
6. **Toggle parity** is asserted: with `ENABLE_HYBRID=false`, `retrieve` returns exactly the F3
   dense-only result (same chunks/order) for a fixed mocked index (AC-11).
7. **Eval gate:** `docs/eval_results/f5-hybrid-after.md` and
   `docs/eval_results/f5-hybrid-after-vs-baseline.md` are committed; the delta table shows hit@5 /
   MRR (overall + per slice, especially `table_lookup` and `code_switched`) vs `baseline` (AC-20).
8. Every AC above is covered by an automated test — this list is the test list, not aspiration.

---

## 5. Out of scope (do not implement here)

- **Reranking (F6):** F5 exposes the 12-candidate fused pool with scores; it does **not** re-order by
  a cross-encoder. `retrieve` returns `k=5`; F6 will consume the 12 and cut to 5.
- **Query rewrite / condensation (F7), compression (F8), caching (F9), memory (F17):** F5 touches
  only the retrieval step; the F9 cache key change (standalone question) and all downstream flags are
  untouched.
- **Rebuilding / enriching `bm25.pkl`:** F5 reads the existing F2 artifact as-is and hydrates
  sparse-only hits via Pinecone `fetch`; it deliberately does **not** change
  `app.indexing.bm25.build_and_pickle` or force a re-index/re-embed (blast-radius and cost note in
  design §2).
- **A dense↔namespace map for global BM25:** single-namespace hybrid is best-effort (design §5,
  Edge cases); the default and eval path is `namespace=None` (fan-out both), which is exact.
- **Per-request weighting of dense vs sparse:** RRF is unweighted (rank-only) in F5; weighted fusion
  is a possible later tuning, not part of this gate.
