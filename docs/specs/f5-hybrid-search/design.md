# F5 — Hybrid Search (BM25 + Dense + RRF) · design.md

**Module:** `backend/app/rag/hybrid.py` (+ a body swap in `backend/app/rag/retriever.py`)
**Depends on:** F3, F4 · **Flag:** `ENABLE_HYBRID` · **Eval gate:** `baseline` → `f5-hybrid-after`

---

## 1. Module layout

```
backend/app/rag/
├── retriever.py       # CHANGED: retrieve() becomes a thin dispatcher over the retrieval mode;
│                      #          the existing dense body is kept as dense_retrieve() (F3's code, renamed)
├── hybrid.py          # NEW: BM25 load/score, Pinecone fetch-hydration, RRF fusion, degraded fallback
├── flags.py           # NEW (tiny): apply_flags(settings, flags) overlay — the single toggle wiring
├── refusal.py         # CHANGED: pre_llm_gate uses max(dense_score) over the fused set (AC-15)
├── baseline.py        # CHANGED (additive): overlay flags→settings; read degraded onto AnswerResponse
├── context.py / prompt.py / citations.py / events.py / schemas.py   # UNCHANGED
└── ...
backend/app/core/
├── contracts.py       # CHANGED (additive): AnswerResponse.degraded: bool = False
└── settings.py        # CHANGED (additive): the F5 keys (§7)
backend/app/evals/
└── retrieval.py       # CHANGED (one line): overlay flags→settings before calling the retrieve seam
```

Canonical models (`RetrievedChunk`, `AnswerResponse`, `PipelineFlags`) live in
`app.core.contracts` and are imported, never redefined. `urdu_safe_tokenize` is imported from
`app.indexing.bm25` (reused verbatim — the tokenizer MUST match the one that built the corpus).

---

## 2. Key design decision: read the existing `bm25.pkl`, hydrate sparse hits via Pinecone `fetch`

`bm25.pkl` (built by `app.indexing.bm25.build_and_pickle`) stores only
`{"bm25": BM25Okapi, "chunk_ids": [...]}` — the ranker plus the parallel `chunk_id` list, **not** the
chunk texts/metadata. Dense retrieval returns fully-hydrated `RetrievedChunk`s (F2 wrote `text`,
`title`, `section_heading`, `page_start/end`, `anchor` into Pinecone metadata). Sparse retrieval
returns only `chunk_id`s and needs those same fields to build a `RetrievedChunk`.

**Two options were weighed:**

| Option | Mechanism | Rejected because / Chosen because |
|---|---|---|
| **A — enrich the pickle** | Change `build_and_pickle` to store text+metadata+namespace per chunk; sparse retrieval self-contained (true LangChain `BM25Retriever`). | **Rejected:** touches the F2-owned artifact *and* forces `python -m app.indexing.run`, which **re-embeds the whole corpus** (real OpenAI \$ + changes the index manifest), inflating F5's blast radius and muddying the `baseline`→`f5` comparison. |
| **B — read pkl as-is, `fetch` sparse-only ids** ✅ | Score BM25 from the existing pkl; hydrate the sparse-only `chunk_id`s (those dense didn't already return) with an async `index.fetch(ids, namespace)`; metadata is already there from F2. | **Chosen:** purely additive, no re-index/re-embed, no F2 change; keeps the `retrieve` seam **session-free** (no Postgres session added to the signature — hydration comes from Pinecone metadata, not `chunks`/`documents`); namespace filtering falls out of `fetch` for free (§5). |

The custom-fusion requirement (AC-10) already means we are not using `EnsembleRetriever`, so we gain
nothing from forcing a true `BM25Retriever`; a thin scored wrapper over `BM25Okapi` that exposes
ranks is what fusion actually needs.

---

## 3. Data-flow diagram

```
  retrieve(query, k, namespace, settings)          # seam signature UNCHANGED (AC-16)
        │
        ├─ mode = resolve_mode(settings)            # RETRIEVAL_MODE override else ENABLE_HYBRID (AC-11/13)
        │
        ├─ dense_only  ─► dense_retrieve(...)                       # F3's exact body (baseline path)
        ├─ bm25_only   ─► sparse_retrieve(...) ─► hydrate ─► [:k]   # eval diagnostic
        └─ hybrid ─►
                 ┌───────────────────────────── asyncio.gather ─────────────────────────────┐
                 │  dense_retrieve(query, HYBRID_DENSE_TOP_K=20, namespace, settings)         │  await (I/O)
                 │        → list[RetrievedChunk] w/ dense_score + dense rank                   │
                 │  sparse_retrieve(query, HYBRID_SPARSE_TOP_K=20, settings)                   │  inline CPU
                 │        → [(chunk_id, sparse_score, sparse_rank)]                            │
                 └──────────────────────────────────────────────────────────────────────────┘
                 │   (dense raises/timeout while hybrid?) ──► DEGRADED: BM25-only, set degraded ctxvar (AC-14)
                 ▼
        hydrate sparse-only ids:  index.fetch(ids=sparse∖dense, namespace)   # await (I/O), metadata from F2
                 ▼
        rrf_fuse(dense_list, sparse_list, k=HYBRID_RRF_K=60)                  # inline CPU
                 │   dedupe by chunk_id (AC-6)
                 │   fused_score = Σ 1/(60 + rank_i)                          (AC-7)
                 │   populate dense_score / sparse_score / fused_score        (AC-8)
                 │   sort by fused_score desc, cap at HYBRID_FUSED_TOP_K=12    (AC-9)
                 ▼
        return fused[:k]     # k=5 to generation (AC-9); the 12-pool is what hybrid_retrieve exposes for F6
                 │
                 ▼
  refusal.pre_llm_gate(chunks, settings)   # CHANGED: max(dense_score over chunks) < threshold (AC-15)
                 ▼
  … unchanged F3 pipeline (format_context | prompt | llm | parser → citations → meta) …
```

**Async-mandate placement (stated per CLAUDE.md "which side of the line"):** the one-time
`bm25.pkl` pickle load is offloaded via `anyio.to_thread.run_sync` (AC-1); every Pinecone call
(dense query, namespace fan-out, sparse-hit `fetch`) is awaited; `urdu_safe_tokenize`,
`BM25Okapi.get_scores` (O(~600 chunks) numpy), and the RRF dict math run **inline** as cheap
pure-CPU — the same side of the line as the cache-matrix cosine matmul the mandate permits inline.
No sync twin (`invoke`/`embed_query`/blocking `requests`/sync `redis`) appears in `hybrid.py`.

---

## 4. Key function signatures

```python
# app/rag/hybrid.py

_BM25_CACHE: dict | None = None          # module-level, loaded once (AC-1)
_DEGRADED: contextvars.ContextVar[bool] = contextvars.ContextVar("hybrid_degraded", default=False)

async def load_bm25(settings) -> dict:
    """anyio.to_thread.run_sync(pickle.load) once; cache. Missing/unreadable file → HybridIndexError
    naming settings.BM25_PATH (fail fast, AC-2)."""

def sparse_scores(query: str, bm25_cache: dict, top_k: int) -> list[tuple[str, float, int]]:
    """urdu_safe_tokenize(query) → BM25Okapi.get_scores → top_k (chunk_id, sparse_score, rank).
    Inline pure-CPU (AC-3). rank is 1-indexed by descending BM25 score."""

async def hydrate_sparse_only(
    ids: list[str], namespace: str | None, settings,
) -> dict[str, RetrievedChunk]:
    """index.fetch(ids, namespace) for ids NOT already in the dense set; build RetrievedChunk from the
    F2 metadata (undo the -1 page sentinel, same helper shape as retriever._none_if_sentinel).
    namespace=None → fetch across settings.RETRIEVAL_NAMESPACES and merge (AC-4, §5)."""

def rrf_fuse(
    dense: list[RetrievedChunk],
    sparse: list[tuple[str, float, int]],
    sparse_chunks: dict[str, RetrievedChunk],
    settings,
) -> list[RetrievedChunk]:
    """Dedupe by chunk_id (AC-6); fused_score = Σ 1/(HYBRID_RRF_K + rank) over the lists a chunk is in
    (AC-7); populate dense/sparse/fused_score, None where absent (AC-8); sort desc, cap at
    HYBRID_FUSED_TOP_K (AC-9). Pure-CPU, inline. Dense rank = position in `dense` (1-indexed)."""

async def hybrid_retrieve(
    query: str, k: int, namespace: str | None, settings,
) -> list[RetrievedChunk]:
    """dense (top-20) ∥ sparse (top-20) → hydrate → rrf_fuse → up to 12 candidates. On dense failure
    while hybrid: BM25-only + _DEGRADED.set(True) (AC-14). Returns the fused pool (≤12); the seam
    truncates to k. F6 calls this directly for the 12-candidate pool (US-6)."""

def was_degraded() -> bool:          # read+reset the ctxvar; baseline.py sets AnswerResponse.degraded
    ...

class HybridIndexError(RuntimeError): ...   # AC-2

# app/rag/retriever.py  — the seam, signature UNCHANGED (AC-16)
def resolve_mode(settings) -> str:   # RETRIEVAL_MODE override else ("hybrid" if ENABLE_HYBRID else "dense_only")
async def dense_retrieve(query, k, namespace, settings) -> list[RetrievedChunk]:  # F3's current body, renamed
async def retrieve(query, k, namespace, settings) -> list[RetrievedChunk]:
    mode = resolve_mode(settings)
    if mode == "dense_only": return await dense_retrieve(query, k, namespace, settings)
    if mode == "bm25_only":  return (await hybrid.sparse_only(query, k, namespace, settings))
    fused = await hybrid.hybrid_retrieve(query, k, namespace, settings)
    return fused[:k]

# app/rag/flags.py  — the single toggle overlay (AC-12)
def apply_flags(settings, flags):    # settings.model_copy(update={"ENABLE_HYBRID": flags.hybrid})
    ...
```

`dense_retrieve` is F3's existing `retrieve` body verbatim (the `_build_store` /
`_retrieve_namespace` / `_merge_top_k` helpers move with it, unchanged) — renamed so `retrieve`
becomes the dispatcher. With `ENABLE_HYBRID=false` and `RETRIEVAL_MODE=None`, `retrieve` calls
`dense_retrieve` and is byte-for-byte the `baseline` path (AC-11).

---

## 5. Namespace filtering of a global BM25 index (edge case, explicit)

`bm25.pkl` is **global** — `app.indexing.run` builds it from every indexed chunk across both `pu`
and `hec` (`corpus_ids` accumulates from all docs), and stores no per-chunk namespace. Dense
retrieval, by contrast, is namespace-scoped. F5 reconciles this at hydration time:

- **`namespace=None` (default, and the F4 retrieval-suite path):** fetch the sparse-only candidate
  ids from **each** of `settings.RETRIEVAL_NAMESPACES`; every id resolves in exactly one namespace,
  so the merge is exact and complete. This is the primary path — no accuracy loss.
- **single namespace (e.g. `"pu"`):** fetch the sparse candidate ids from that namespace only; ids
  that live in the other namespace return empty from `fetch` and are dropped. Filtering is therefore
  **exact** (no cross-namespace leakage) but **best-effort on recall** — a query whose global BM25
  top-20 is dominated by the other namespace may yield fewer than 20 sparse hits. Documented as an
  accepted F5 limitation; a per-chunk namespace map in the pickle is deferred (out of scope §5,
  requirements) since it would require the re-index rejected in §2.

---

## 6. Error handling

| Failure | Detection | Handling |
|---|---|---|
| `bm25.pkl` missing/unreadable | `load_bm25` open/pickle raises | raise `HybridIndexError(path)` — fail fast, no silent dense-only (AC-2) |
| Pinecone dense query fails/times out (hybrid on) | exception from `dense_retrieve` inside `hybrid_retrieve` | catch, `_DEGRADED.set(True)`, structlog `hybrid.degraded`, return BM25-only hydrated results; `AnswerResponse.degraded=true` (AC-14) |
| Pinecone `fetch` (hydration) partial/empty for an id | id absent in the fetched namespace | drop that sparse-only id (it can't be shown without metadata); dense/overlapping hits unaffected (§5) |
| Both dense fails **and** BM25 empty | degraded path returns `[]` | falls into F3's existing empty-retrieval → `pre_llm_gate` refusal (`dense_score=-inf`), no new special case |
| Sparse-only chunk at fused rank 0 (dense_score None) | fusion ordering | refusal gate uses `max(dense_score)` over the pool, not `chunks[0]` (AC-15) — no spurious refusal |
| Namespace fan-out one side raises (dense) | F3's `asyncio.gather` (no `return_exceptions`) | unchanged F3 behaviour: propagates → caught by the hybrid degraded handler above |

Dense-provider 429/5xx retries remain handled by F3's `errors.call_with_retry` wrapper around the
`retrieve` call in `_pipeline_events` — F5 does not duplicate that retry logic; degraded fallback
fires only after the retry budget is exhausted.

---

## 7. New Settings keys (central `app.core.settings.Settings`)

```python
# --- Hybrid search (F5) ---
ENABLE_HYBRID: bool = False                    # prod/request toggle; false ≡ F3 dense-only (AC-11)
RETRIEVAL_MODE: Literal["dense_only", "bm25_only", "hybrid"] | None = None  # eval override, wins over ENABLE_HYBRID (AC-13)
HYBRID_DENSE_TOP_K: int = 20                    # dense candidates before fusion (AC-5)
HYBRID_SPARSE_TOP_K: int = 20                   # BM25 candidates before fusion (AC-3)
HYBRID_FUSED_TOP_K: int = 12                    # fused pool cap exposed to F6 (AC-9)
HYBRID_RRF_K: int = 60                          # RRF constant, score = Σ 1/(60 + rank) (AC-7)
# BM25_PATH is reused verbatim from F2 (app/data/bm25.pkl) — NOT redefined.
```

`ENABLE_HYBRID` is added to the feature-flag block alongside the other CLAUDE.md flags. All six keys
carry defaults so `Settings()` still boots without any new env for the dense-only default.

---

## 8. Alembic migrations

**None.** F5 changes only Pydantic contracts and in-memory retrieval:

- `RetrievedChunk` already carries `sparse_score`/`fused_score` (F3 reserved them, contracts.py
  §"F5/F6 populate … without a schema change") — F5 is the first to populate them, no field added.
- `AnswerResponse.degraded: bool = False` is a **transient response contract**, never persisted to a
  table — additive, default-`false`, backward-compatible, so no migration and no existing test breaks.
- `bm25.pkl` is a file artifact, not a DB object; F5 reads it unchanged.
- `eval_runs`/`eval_results` already exist (F12-owned) — the gate persists through F4's existing
  writer with no schema change.

Stated explicitly (same convention F3/F4 used) so a reviewer does not expect a migration.

---

## 9. Toggle wiring — the two overlay call sites (AC-12), and why they are minimal

The F4 retrieval suite calls `retriever.retrieve(rec.question, k, None, settings)` **directly** and
its existing comment already reserves the contract: *"retrieve() reads toggles from settings
(F5+)"*. F5 fulfils that with one shared helper, `flags.apply_flags(settings, flags)`
(`settings.model_copy(update={"ENABLE_HYBRID": flags.hybrid})`), applied at exactly two seams:

1. **Request path — `baseline._pipeline_events`:** overlay `settings = apply_flags(settings, flags)`
   once, before retrieval. This also covers the F4 **ragas / refusal / latency** suites, since they
   drive retrieval through `answer()` → `_pipeline_events` and already pass `flags`.
2. **Eval retrieval suite — `evals/retrieval.run_retrieval`:** overlay `settings = apply_flags(
   settings, flags)` before the direct `retrieve(...)` call. This is the only suite that bypasses
   `answer()`, and it *already receives* `flags` — a one-line addition, not a change to how it
   measures (still calls the same seam, still scores hit@k/MRR the same way). This is the wiring the
   F4 comment reserved, so "F5+ needs no F4 change" holds in spirit: no suite's measurement logic
   changes; a toggle the harness already parsed is simply honoured.

The `bm25_only` diagnostic mode is not expressible as a boolean `PipelineFlags.hybrid`, so it is
selected via `RETRIEVAL_MODE=bm25_only` in the run's env (AC-13) — an eval-only override outside the
standard flag, used for the `dense_only` vs `bm25_only` vs `hybrid` A/B, not the prod toggle.

---

## 10. Honoring the Shared Context contracts & the F3 seam

- **`RetrievedChunk`:** F5 populates `dense_score` (from Pinecone), `sparse_score` (raw BM25), and
  `fused_score` (RRF) on the same model F3 introduced — no schema change (contracts.py already
  reserved these for "F5/F6"). `rerank_score` stays `None` until F6.
- **The F3→F5 seam:** `retrieve(query, k, namespace, settings) -> list[RetrievedChunk]` is unchanged
  in signature and return type (AC-16); F5 swaps the **body** (dispatcher) exactly as F3 §5
  anticipated. `format_context`, `prompt.build_prompt`, `parse_citations`, `SSEEvent`, and the stage
  vocabulary (`searching`/`generating`/`citing`) are untouched.
- **`StageEvent` / SSE contract:** unchanged — F5 adds no stage; the `searching` stage now does more
  work internally but emits the identical `started`/`done` pair. (Degraded is reported on the final
  `meta.degraded`, not as a new stage, keeping the ordered contract stable for F14.)
- **`AnswerResponse`:** gains `degraded` (additive); every other field is set exactly as F3 sets it.
- **Refusal contract ("refusal, not hallucination"):** preserved and made fusion-safe (AC-15) — the
  gate still refuses genuinely low-confidence retrieval, just measured over the whole fused pool's
  dense scores rather than a single position that fusion may have reordered.
- **Cost rule:** F5 adds **no** OpenAI call beyond F3's single query embedding (the same
  `aembed_query` dense retrieval already makes); BM25 is free/in-process — so there is no new
  `estimate_cost` site, stated explicitly. The eval-gate's RAGAS/latency suites log cost through F4's
  existing `estimate_cost` path unchanged.
- **Toggle rule:** `ENABLE_HYBRID` (config) + `PipelineFlags.hybrid` (request/eval) make F5 fully
  A/B-able and instantly roll-back-able to the identical `baseline` code path (AC-11/US-3).
```
