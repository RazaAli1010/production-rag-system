# F6 — Cross-Encoder Reranking · design.md

**Module:** `backend/app/rag/rerank.py` (+ a rerank step in the `retriever.retrieve` seam body)
**Depends on:** F5, F4 · **Flag:** `ENABLE_RERANK` · **Eval gate:** `f5-hybrid-after` →
`f6-rerank-after`

---

## 1. Module layout

```
backend/app/rag/
├── rerank.py          # NEW: cross-encoder singleton, direct score() offload, sigmoid calibration,
│                      #      whole-object reorder+slice, LangChain API surface (off-path), warm hook
├── retriever.py       # CHANGED: retrieve() gains a rerank step over the candidate pool before [:k]
├── flags.py           # CHANGED (one key): apply_flags also maps flags.rerank -> ENABLE_RERANK
├── refusal.py         # CHANGED: pre_llm_gate uses max(rerank_score) when rerank on, else F5 dense gate
├── observability.py   # CHANGED (additive): log_rerank(rerank_ms, max_score, n_candidates)
├── hybrid.py          # UNCHANGED: hybrid_retrieve already returns the fused ≤12 pool F6 consumes (US-6)
├── baseline.py        # UNCHANGED: retrieve() already returns the top-k chunks the gate reads
├── context.py / prompt.py / citations.py / events.py / schemas.py   # UNCHANGED
└── ...
backend/app/core/
└── settings.py        # CHANGED (additive): the F6 keys (§7)
backend/app/evals/
└── retrieval.py       # UNCHANGED: already overlays apply_flags(settings, flags) before retrieve()
```

Canonical models (`RetrievedChunk`, `PipelineFlags`) live in `app.core.contracts` and are imported,
never redefined. `rerank_score` is already a field on `RetrievedChunk` (reserved for "F5/F6") — F6
is the first to populate it, so **no contract field is added and no migration is created** (§8).

---

## 2. Key design decision: rerank **inside** the seam, over F5's existing fused pool

CLAUDE.md's pipeline order is `hybrid retrieve (F5) → rerank (F6) → refusal gate`. Two placements
were weighed:

| Option | Mechanism | Rejected / Chosen |
|---|---|---|
| **A — rerank as a stage in `_pipeline_events`** | After the `searching` stage returns `k=5`, rerank in `baseline.py` between retrieval and the refusal gate. | **Rejected:** F5's seam already truncates to `k=5`, so there would be nothing to re-order (rerank needs the 12-pool, not 5); and the F4 **retrieval suite** calls `retrieve` directly, so it would score the *un*-reranked order — breaking the eval-gate re-measurement that F5's design relies on. |
| **B — rerank inside `retrieve`, over the fused pool, before `[:k]`** ✅ | `retrieve` gets the fused ≤12 pool from `hybrid_retrieve` (which F5 already returns un-truncated for exactly this, US-6), reranks, returns the top `RERANK_TOP_N`. | **Chosen:** rerank sees the full 12 candidates; the retrieval suite re-measures hit@k/MRR over the reranked order with **zero F4 change** (same property F5 used); the refusal gate downstream reads `rerank_score` off the returned chunks with no new plumbing; seam signature and SSE contract stay identical (AC-19). |

Because `hybrid_retrieve` already returns the fused pool **before** the seam's `fused[:k]`
truncation, F6 needs no change to F5's fusion, `bm25.pkl`, or the dense index — so
`f5-hybrid-after` numbers stay directly comparable (no re-index, no re-embed; blast radius is one
new module plus a rerank step in the dispatcher).

**Direct path vs LangChain (resolved in the brief, restated):** the runtime rerank calls
`HuggingFaceCrossEncoder.score(pairs)` **directly** to get raw per-pair scores for
`rerank_score` + the calibrated gate; `CrossEncoderReranker.compress_documents` discards scores and
`ContextualCompressionRetriever` re-retrieves, so both are built only as demonstrable LangChain API
surface (AC-3), over the same shared model, and never invoked on the request path.

---

## 3. Data-flow diagram

```
  retrieve(query, k, namespace, settings)              # seam signature UNCHANGED (AC-19)
        │
        ├─ mode = resolve_mode(settings)               # F5: dense_only | bm25_only | hybrid
        │
        ├─ pool = candidate pool for the mode:
        │        hybrid    ─► hybrid_retrieve(...)                    → ≤ HYBRID_FUSED_TOP_K (12)
        │        dense_only─► dense_retrieve(query, pool_k, ns, s)    → top pool_k   (diagnostic)
        │        bm25_only ─► hybrid.sparse_only(query, pool_k, ns,s) → top pool_k   (diagnostic)
        │        (pool_k = RERANK_CANDIDATE_K when rerank on, else k)
        │
        ├─ ENABLE_RERANK == false ──► return pool[:k]                 # byte-for-byte F5 (AC-17)
        │
        └─ ENABLE_RERANK == true  ──►
                 rerank.rerank_chunks(query, pool, settings)
                 ┌──────────────────────────────────────────────────────────────────┐
                 │  pool empty?  ─► return [] , max_rerank_score=0, rerank_ms=0 (AC-14)│ short-circuit
                 │  pairs = [(query, safe_text(c)) for c in pool]     (guard AC-15)    │ inline
                 │  logits = await anyio.to_thread.run_sync(hf.score, pairs)  (AC-4)   │ OFF-LOOP
                 │  scores = sigmoid(logits) if RERANK_APPLY_SIGMOID else logits (AC-10│ inline CPU
                 │  bind: chunk.rerank_score = score  (per whole object, AC-9)         │ inline
                 │  reorder WHOLE objects by rerank_score desc; slice top RERANK_TOP_N │ inline
                 │  log_rerank(rerank_ms, max_rerank_score, len(pool))       (AC-20)   │ structlog
                 └──────────────────────────────────────────────────────────────────┘
                 ▼
        return reranked_top_n            # count to generation stays 5 (AC-6)
                 │
                 ▼
  refusal.pre_llm_gate(chunks, settings)  # CHANGED: rerank on → max(rerank_score) < REFUSAL_RERANK_THRESHOLD (AC-12)
                 ▼
  … unchanged F3/F5 pipeline (format_context | prompt | llm | parser → citations → meta) …
```

**Async-mandate placement (CLAUDE.md "which side of the line"):** the one-time model load (AC-2)
and the per-request `score` forward pass (AC-4) are the two `anyio.to_thread.run_sync` offloads —
both blocking/CPU-bound. PyTorch releases the GIL during the forward pass, so the worker thread
yields real concurrency (the loop-lag probe, AC-5). The sigmoid over ≤12 floats and the sort/slice
run **inline** as cheap pure-CPU (the same side of the line as F5's RRF math / the cache-matrix
cosine). No sync twin appears in `rerank.py` (AC-22).

---

## 4. Key function signatures

```python
# app/rag/rerank.py

_RERANK_MODEL = None                          # HuggingFaceCrossEncoder singleton, loaded once (AC-1)
_MODEL_LOCK = asyncio.Lock()                  # guards the one-time load under concurrent first use (AC-2)

async def get_rerank_model(settings):
    """Return the shared HuggingFaceCrossEncoder, loading it once off-loop under the lock:
    HuggingFaceCrossEncoder(model_name=settings.RERANK_MODEL,
                            model_kwargs={"device": settings.RERANK_DEVICE})  # device pinned 'cpu' (AC-1)
    Construction (weight load / first-use download) runs via anyio.to_thread.run_sync (AC-2)."""

async def warm_rerank_model(settings) -> None:
    """Preload hook for F11's startup lifespan (out of scope to wire here); calls get_rerank_model."""

def _calibrate(logits: list[float], settings) -> list[float]:
    """sigmoid(logit) into [0,1] when settings.RERANK_APPLY_SIGMOID, else pass through already-
    activated scores (AC-10/AC-11). Inline pure-CPU (math.exp over ≤12 floats)."""

def _safe_text(chunk: RetrievedChunk) -> str:
    """Guard empty/whitespace-only text so a degenerate pair can't poison the batch (AC-15)."""

async def rerank_chunks(
    query: str, pool: list[RetrievedChunk], settings,
) -> list[RetrievedChunk]:
    """Direct-path rerank (the runtime path). Empty pool → [] short-circuit, no model call (AC-14).
    Otherwise: build (query, text) pairs (AC-15 guard); one batched
    `logits = await anyio.to_thread.run_sync(model.score, pairs)` (AC-4/AC-7); calibrate (AC-10);
    bind rerank_score onto each WHOLE chunk object (AC-9); sort desc + slice top RERANK_TOP_N
    (AC-6/AC-9); log_rerank(rerank_ms, max_rerank_score, len(pool)) (AC-8/AC-20). Returns the
    reranked top-N (rerank_score populated; other scores carried through from F5)."""

def build_compression_retriever(base_retriever, settings):
    """API surface ONLY (AC-3), off the runtime path: ContextualCompressionRetriever(
        base_compressor=CrossEncoderReranker(model=<shared get_rerank_model result>,
                                             top_n=settings.RERANK_TOP_N),
        base_retriever=base_retriever)."""

class HybridBaseRetriever(BaseRetriever):
    """Thin BaseRetriever adapter over hybrid.hybrid_retrieve so the LangChain compression retriever
    (AC-3) has a base_retriever; converts RetrievedChunk -> Document. Test-only wiring."""
    async def _aget_relevant_documents(self, query, *, run_manager): ...

def max_rerank_score(chunks: list[RetrievedChunk]) -> float:
    """max(c.rerank_score for c if not None), else 0.0 — the calibrated confidence for the gate (AC-10)."""

# app/rag/retriever.py  — seam body, signature UNCHANGED (AC-19)
async def retrieve(query, k, namespace, settings) -> list[RetrievedChunk]:
    mode = resolve_mode(settings)
    pool_k = settings.RERANK_CANDIDATE_K if settings.ENABLE_RERANK else k
    if mode == "dense_only":
        pool = await dense_retrieve(query, pool_k, namespace, settings)
    elif mode == "bm25_only":
        from app.rag import hybrid
        pool = await hybrid.sparse_only(query, pool_k, namespace, settings)
    else:
        from app.rag import hybrid
        pool = await hybrid.hybrid_retrieve(query, k, namespace, settings)   # already ≤12
    if settings.ENABLE_RERANK:
        from app.rag import rerank
        return await rerank.rerank_chunks(query, pool, settings)
    return pool[:k]

# app/rag/flags.py  — one added key (AC-18)
def apply_flags(settings, flags):
    return settings.model_copy(update={
        "ENABLE_HYBRID": flags.hybrid,
        "ENABLE_RERANK": flags.rerank,     # F6 addition
    })

# app/rag/refusal.py  — gate swap (AC-12)
def pre_llm_gate(chunks, settings) -> bool:
    if not chunks:
        return True
    if settings.ENABLE_RERANK:
        return rerank.max_rerank_score(chunks) < settings.REFUSAL_RERANK_THRESHOLD
    dense = [c.dense_score for c in chunks if c.dense_score is not None]   # F5 path unchanged
    return (max(dense) if dense else float("-inf")) < settings.REFUSAL_DENSE_THRESHOLD
```

`resolve_mode`, `dense_retrieve`, `hybrid_retrieve`, `sparse_only` are F5's, unchanged. With
`ENABLE_RERANK=false` and `RETRIEVAL_MODE=None`, `retrieve` returns `pool[:k]` exactly as F5 does
(AC-17). The `pool_k` widen-when-rerank-on covers the diagnostic `dense_only`/`bm25_only` + rerank
runs so rerank always has a pool larger than `k` to re-order; hybrid already returns ≤12.

---

## 5. Verifying the activation (AC-11) — how the sanity check is wired

`sentence-transformers` can apply a default activation read from the model config, so
`HuggingFaceCrossEncoder.score` may return **raw logits** or an **already-sigmoided** value. Applying
sigmoid twice silently corrupts calibration and the tuned threshold. The verification is a one-time
implementation task (tasks T6), not a runtime cost:

1. Score one clearly-relevant pair (e.g. the query against its own answer chunk) and one clearly-
   irrelevant pair.
2. If both raw outputs lie in `[0, 1]` → the model already activated; set
   `RERANK_APPLY_SIGMOID=false` and treat scores as calibrated directly.
3. If outputs are unbounded / can be negative → raw logits; keep `RERANK_APPLY_SIGMOID=true`.

The default in Settings is `RERANK_APPLY_SIGMOID=true` (raw-logit assumption for
`ms-marco-MiniLM-L-6-v2`, whose head emits a single unbounded logit); the T6 sanity check confirms
or flips it, and a unit test pins the resulting calibrated range so a future model/version change
that alters the activation fails loudly.

---

## 6. Error handling

| Failure | Detection | Handling |
|---|---|---|
| Empty candidate pool (F5 returned `[]`, e.g. degraded + BM25 empty) | `len(pool) == 0` | short-circuit: return `[]`, `max_rerank_score=0`, `rerank_ms=0`, **no** model call (AC-14) → existing empty-retrieval refusal |
| Whitespace-only / empty chunk text | `_safe_text` guard | substitute a safe placeholder (or floor the pair's score) so the batch `score` call cannot break (AC-15) |
| Model weights missing at runtime (`HF_HUB_OFFLINE=1`, not baked) | `get_rerank_model` load raises | propagate a clear startup/first-use error (fail fast, like F5's `HybridIndexError`); the Docker build step (AC-16, F15) prevents this in prod |
| Concurrent first-request load race | `_MODEL_LOCK` + None re-check inside the lock | only one thread constructs the model; others await and reuse (AC-2) |
| Rerank raises mid-request (unexpected torch error) | exception in `rerank_chunks` | propagates to `retrieve`, caught by F3's existing `errors.call_with_retry` / `_pipeline_events` try-block → terminal SSE `error` (no new special case) |

Reranking adds **no** OpenAI call and no new network dependency (weights are local), so there is no
new `estimate_cost` site — stated explicitly (mirrors F5's cost note). The only new cost is CPU time,
bounded by AC-8 and logged as `rerank_ms`.

---

## 7. New Settings keys (central `app.core.settings.Settings`)

```python
# --- Reranking (F6) ---
ENABLE_RERANK: bool = False                 # prod/request toggle; false ≡ F5 path (AC-17)
RERANK_MODEL: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"   # fixed stack (AC-1)
RERANK_DEVICE: str = "cpu"                   # PINNED cpu so it never auto-selects CUDA/MPS (AC-1)
RERANK_TOP_N: int = 5                        # kept after rerank → generation count (AC-6)
RERANK_CANDIDATE_K: int = 12                 # pool size fed to rerank (matches HYBRID_FUSED_TOP_K)
RERANK_APPLY_SIGMOID: bool = True            # calibration; verified via the T6 sanity check (AC-11)
REFUSAL_RERANK_THRESHOLD: float = 0.5        # calibrated refusal gate, tuned on the F4 refusal suite (AC-13)
# HYBRID_FUSED_TOP_K (F5) is the fused-pool cap reused as the hybrid rerank input — NOT redefined.
```

`ENABLE_RERANK` joins the feature-flag block alongside `ENABLE_HYBRID`. All keys carry defaults so
`Settings()` still boots without new env for the rerank-off default. `RERANK_DEVICE` is a Settings
value (app config); `HF_HUB_OFFLINE` is **not** — it is a Docker/runtime env var (F15, AC-16), read
by the HuggingFace library, not by our code.

---

## 8. Alembic migrations

**None.** F6 changes only in-memory retrieval and Pydantic-field *population*:

- `RetrievedChunk.rerank_score` already exists (F3 reserved it for "F5/F6"); F6 populates it — no
  field added.
- `AnswerResponse` is unchanged (no `rerank`-specific response field; `degraded` etc. untouched).
- The cross-encoder weights are a file/cache artifact, not a DB object.
- `eval_runs`/`eval_results` already exist (F12-owned); the gate persists through F4's writer.

Stated explicitly (same convention F3/F4/F5 used) so a reviewer does not expect a migration; T10
asserts `alembic` autogenerate is empty.

---

## 9. Toggle wiring — one extended overlay, no new call site (AC-18)

F5 introduced `rag.flags.apply_flags(settings, flags)` and applied it at exactly two seams:
`baseline._pipeline_events` (request path + the ragas/refusal/latency suites via `answer()`) and
`evals.retrieval.run_retrieval` (the one suite calling the seam directly). F6 reuses both call sites
verbatim and only **extends the overlay** to also map `flags.rerank -> ENABLE_RERANK`:

```python
return settings.model_copy(update={"ENABLE_HYBRID": flags.hybrid, "ENABLE_RERANK": flags.rerank})
```

No suite's measurement logic changes: the retrieval suite still calls the same `retrieve` seam and
scores hit@k/MRR the same way — it simply now sees a reranked order because `ENABLE_RERANK` is on.
This is why "F5+ needs no F4 change" continues to hold for F6. `RERANK_CANDIDATE_K`/`RERANK_TOP_N`
are not per-request flags (no `PipelineFlags` field), so they stay pure Settings values driven by the
run's env — matching how F5 handled `RETRIEVAL_MODE`.

---

## 10. Honoring the Shared Context contracts & the F3/F5 seam

- **`RetrievedChunk`:** F6 populates `rerank_score` on the same model F3 introduced and F5 extended
  (`dense_score`/`sparse_score`/`fused_score` carry through the reorder untouched) — no schema
  change (contracts.py already reserved `rerank_score` for "F5/F6"). Whole objects are reordered so
  every score + citation field stays bound to its chunk through sort and slice (AC-9).
- **The F3→F5→F6 seam:** `retrieve(query, k, namespace, settings) -> list[RetrievedChunk]` is
  unchanged in signature and return type (AC-19); F6 adds a step to the **body** exactly as F5 added
  the dispatcher, precisely the "swap the retrieval step without touching prompt, parsing, or
  streaming" property F3 §5 reserved.
- **`StageEvent` / SSE contract:** unchanged — F6 adds **no** stage (reranking is folded into the
  existing `searching` stage, mirroring F5's decision), so the ordered
  `stage* → token* → citations → meta → done|error` contract is stable for F14/F17. `rerank_ms` is
  reported via structlog/Langfuse, not as a new SSE field.
- **Refusal contract ("refusal, not hallucination"):** preserved and *improved* — the gate now
  refuses on a **calibrated** relevance score (AC-12/AC-13) rather than a raw cosine RRF can inflate,
  tuned on the F4 refusal suite so recall rises without worsening false refusals.
- **Cost rule:** F6 adds no OpenAI call (BM25 was free; the cross-encoder is free/in-process), so no
  new `estimate_cost` site; the eval-gate RAGAS/latency suites log LLM cost through F4's existing
  path unchanged. The only new logged metric is `rerank_ms` (AC-20).
- **Async mandate:** model load + `score` are the two `anyio.to_thread.run_sync` offloads; sigmoid +
  sort run inline; the `rerank.py` async grep-guard stays green (AC-22).
- **Toggle rule:** `ENABLE_RERANK` (config) + `PipelineFlags.rerank` (request/eval) make F6 fully
  A/B-able and instantly roll-back-able to the identical `f5-hybrid-after` code path (AC-17/US-3).
