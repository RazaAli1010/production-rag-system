# F8 — Context Compression & Token Cost Control · design.md

**Module:** `backend/app/rag/compression.py` (a post-refusal / pre-generation step in
`baseline._pipeline_events`)
**Depends on:** F6 (calibrated `rerank_score` + the loaded cross-encoder), F7 (`normalized` query),
F4 · **Flag:** `ENABLE_COMPRESSION` · **Eval gate:** `f7-rewrite-after` → `f8-compression-after`
**No new model:** sentence scoring reuses `rerank.get_rerank_model`
(`cross-encoder/ms-marco-MiniLM-L-6-v2`); token counting is tiktoken `cl100k_base`. No OpenAI call.

---

## 1. Module layout

```
backend/app/rag/
├── compression.py      # NEW: relevance_floor, dedupe (5-gram Jaccard), token-budget greedy fill +
│                       #      sentence-level trim (F6 cross-encoder, batched, off-loop),
│                       #      compress_chunks() orchestrator + metrics, build_document_compressor()
│                       #      (LangChain API surface only, off the request path)
├── baseline.py         # CHANGED: _pipeline_events runs compression.compress_chunks after the
│                       #      refusal gate and before chain_input (flag-gated; scoring query from
│                       #      last_rewrite())
├── flags.py            # CHANGED (one key): apply_flags also maps flags.compression -> ENABLE_COMPRESSION
├── observability.py    # CHANGED (additive): log_compression(tokens_before, tokens_after,
│                       #      chunks_before, chunks_after, sentences_dropped, compression_ms)
├── rerank.py           # UNCHANGED: get_rerank_model reused for sentence scoring (one shared model)
├── context.py          # UNCHANGED: format_context numbers the compressed list; extract_quote reads
│                       #      trimmed text verbatim
├── refusal.py          # UNCHANGED: pre_llm_gate runs BEFORE compression on the full reranked set
├── retriever.py / rewrite.py / hybrid.py / citations.py / events.py / schemas.py   # UNCHANGED
└── ...
backend/app/core/
└── settings.py         # CHANGED (additive): the F8 keys (§7)
backend/app/evals/
└── (no change)         # retrieval suite is untouched; RAGAS + latency suites drive answer() → the
                        # compressed generation path automatically once the flag maps through
```

Canonical models (`RetrievedChunk`, `PipelineFlags`, `MemoryContext`, `RewriteResult`) live in
`app.core.contracts` and are imported, never redefined. `PipelineFlags.compression` already exists and
`parse_flags` already accepts it. **No** new contract model and **no** new field on an existing one —
compression works entirely over transient `RetrievedChunk` copies (§8).

---

## 2. Key design decision: compression on the generation path, after the refusal gate

CLAUDE.md's pipeline order is explicit: `… hybrid retrieve (F5) → rerank (F6) → refusal gate →
compress (F8) → generate (F3 chain) …`. Three placements were weighed:

| Option | Mechanism | Rejected / Chosen |
|---|---|---|
| **A — compress inside the retrieval seam** (`rewrite.retrieve` / `retriever.retrieve`, like F5/F6) | The seam returns a compressed pool. | **Rejected:** (1) the refusal gate reads `max_rerank_score` of the retrieved set and must see the **full** reranked confidence — flooring/trimming before the gate could flip a refusal decision; CLAUDE.md orders the gate *before* compress. (2) The F4 **retrieval suite** calls the seam directly and scores hit@k on it; compressing there would corrupt the retrieval metric with a generation-only transform. |
| **B — compress inside the LCEL generate chain** (a `RunnableLambda` before `format_context`) | `RunnablePassthrough.assign` compresses `chunks` mid-chain. | **Rejected:** the chain input feeds **both** `format_context` and (post-generation) `parse_citations`; compressing inside the chain would desync the two lists, and the off-loop cross-encoder call is awkward to express as a sync `RunnableLambda`. Keeping compression a plain `await` in `_pipeline_events` (before `chain_input` is built) keeps one compressed list for context **and** citations (AC-11). |
| **C — a plain `await compression.compress_chunks(...)` in `_pipeline_events`, after the refusal gate, before `chain_input`** ✅ | `_pipeline_events` reassigns `chunks = await compress_chunks(scoring_query, chunks, settings)` when the flag is on. | **Chosen:** matches the CLAUDE.md order exactly; the refusal gate sees the uncompressed reranked set; the retrieval suite is untouched (hit@k unchanged); one compressed list drives context + citations + the cost-accounting token count; flag-off is byte-for-byte `f7-rewrite-after`. |

**Consequence for the eval gate (call this out at §9):** because compression lives on the generation
path, the **retrieval suite (hit@k / MRR) is identical to `f7-rewrite-after`**. F8 is measured by the
**RAGAS** suite (faithfulness, context_precision — both drive `answer()` → `_pipeline_events` → the
compressed prompt) and the **latency/cost** suite. This is the structural difference from F5/F6/F7,
which were retrieval-seam features the retrieval suite measured directly.

Blast radius: one new module + a flag-gated block in `_pipeline_events` + one `apply_flags` key + one
observability helper. No re-index, no re-embed, no seam signature change.

---

## 3. Data-flow diagram

```
  _pipeline_events(query, k, ns, flags, memory, session, settings)
        │  settings = apply_flags(settings, flags)     # + flags.compression -> ENABLE_COMPRESSION
        │  chunks = await rewrite.retrieve(query, k, ns, settings, memory)   # F7 seam (UNCHANGED)
        │  rewrite_result = rewrite.last_rewrite()      # F7: language + normalized query
        │  stage_event("searching","done")
        ▼
  if refusal.pre_llm_gate(chunks, settings):   ──►  refusal branch (UNCHANGED — runs on FULL reranked set)
        │      (compression NEVER runs on a refused query)
        ▼  (not refused)
  ┌─ ENABLE_COMPRESSION ? ───────────────────────────────────────────────────────────────┐
  │  scoring_query = rewrite_result.normalized if rewrite_result else query        (AC-9)   │
  │  chunks = await compression.compress_chunks(scoring_query, chunks, settings)            │
  │        │  t0 = perf_counter(); tokens_before = Σ count_tokens(c.text)                    │
  │        │  step 1  kept = dedupe(chunks, settings)             # 5-gram Jaccard>0.7 (AC-4) │ inline CPU
  │        │  step 2  kept = relevance_floor(kept, settings)      # score<floor, keep≥MIN (AC-1/2)│ inline CPU
  │        │  step 3  kept, n_sent = await token_budget_fill(scoring_query, kept, settings)  │
  │        │             greedy tokens ≤ BUDGET; overflow chunk sentence-trimmed:            │
  │        │             pairs=[(q,sent)…]; logits=await to_thread.run_sync(model.score,pairs)│ OFF-LOOP (AC-8/15)
  │        │             keep top sentences that fit, ORIGINAL order (AC-8); metadata kept (AC-10)│
  │        │  tokens_after = Σ count_tokens(c.text); log_compression(before,after,…,ms) (AC-12)│ structlog
  │        │  on ANY exception → log 'compression_failed'; return original chunks   (AC-13)   │
  └────────────────────────────────────────────────────────────────────────────────────────┘
        ▼   (chunks is now the compressed list — the SINGLE list used below)
  stage_event("generating","started")
  chain_input = {"chunks": chunks, "memory_block", "question", "language_directive"}   # compressed (AC-11)
  … F3 generation (format_context | prompt | gpt-4o-mini | parser) …                    # fewer input tokens
  tokens_in = len(_ENC.encode(SYSTEM + language + format_context(chunks) + memory + query))   # compressed
  await observability.log_llm_cost(LLM_MODEL, tokens_in, tokens_out)                    # cost win (AC-14)
  resolved_citations = await parse_citations(answer_text, chunks, session)             # SAME compressed list (AC-11)
```

**Async-mandate placement (CLAUDE.md "which side of the line"):** the cross-encoder sentence scoring is
the one off-loop offload (`anyio.to_thread.run_sync(model.score, pairs)`, reusing F6's pattern — PyTorch
releases the GIL during the forward pass). tiktoken counting, the 5-gram/Jaccard set math, the dedupe
comparison over ≤5 chunks, and the greedy fill run **inline** as cheap pure-CPU (the same side of the
line as F5's RRF and F6's sigmoid). No sync twin appears in `compression.py` (AC-15).

---

## 4. Key function signatures

```python
# app/rag/compression.py

import time
import tiktoken
import anyio
import structlog
from app.core.contracts import RetrievedChunk
from app.rag import observability
from app.rag import rerank as rerank_mod

_ENC = tiktoken.get_encoding("cl100k_base")   # local encoder (baseline imports compression indirectly)


def count_tokens(text: str) -> int:
    return len(_ENC.encode(text or ""))


def _ngrams(text: str, n: int) -> set[tuple[str, ...]]:
    words = text.lower().split()
    if len(words) < n:
        return {tuple(words)} if words else set()
    return {tuple(words[i:i + n]) for i in range(len(words) - n + 1)}


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / len(a | b)


def dedupe(chunks: list[RetrievedChunk], settings) -> tuple[list[RetrievedChunk], int]:
    # Walk in rerank order (input order); drop a chunk when its 5-gram Jaccard vs an already-kept,
    # higher-or-equal-scored chunk exceeds COMPRESSION_DEDUPE_JACCARD. Never drop below MIN_CHUNKS.
    ...


def _score_of(c: RetrievedChunk) -> float:
    return c.rerank_score if c.rerank_score is not None else float("-inf")


def relevance_floor(chunks: list[RetrievedChunk], settings) -> tuple[list[RetrievedChunk], int]:
    # Keep chunks with rerank_score >= COMPRESSION_SCORE_FLOOR (or rerank_score is None → kept, AC-3);
    # if fewer than COMPRESSION_MIN_CHUNKS survive, top up from the dropped set by descending score.
    ...


def _split_sentences(text: str) -> list[str]:
    # Regex split on sentence terminators keeping regulation identifiers like "15(3)" intact;
    # returns non-empty stripped sentences in document order.
    ...


async def _trim_chunk(query: str, chunk: RetrievedChunk, budget: int, settings) -> tuple[RetrievedChunk, int]:
    # Split into sentences; if the whole chunk already fits `budget`, return it unchanged (0 dropped).
    # Else score every (query, sentence) pair in ONE batched off-loop call via the F6 cross-encoder,
    # greedily keep the highest-scored sentences whose cumulative tokens fit `budget`, then re-emit the
    # kept sentences in ORIGINAL order. Return a model_copy with only `text` replaced (metadata/scores
    # preserved, AC-10) and the count of dropped sentences.
    model = await rerank_mod.get_rerank_model(settings)
    logits = await anyio.to_thread.run_sync(model.score, pairs)   # AC-8/AC-15
    ...


async def token_budget_fill(query: str, chunks: list[RetrievedChunk], settings) -> tuple[list[RetrievedChunk], int]:
    # Greedy fill in rerank order to COMPRESSION_TOKEN_BUDGET (AC-6). A chunk that fits is added whole;
    # the first overflow chunk is `_trim_chunk`-ed to the remaining budget (AC-7); chunks after it are
    # dropped. The first COMPRESSION_MIN_CHUNKS chunks are always retained (trimmed if needed) so the
    # floor's ≥MIN guarantee survives the budget (AC-7). Returns (kept, total_sentences_dropped).
    ...


async def compress_chunks(query: str, chunks: list[RetrievedChunk], settings) -> list[RetrievedChunk]:
    # Orchestrator: dedupe → relevance_floor → token_budget_fill; logs rag.compression (AC-12).
    # Best-effort: any exception → log 'compression_failed' and return the original chunks (AC-13).
    if not chunks:
        return chunks
    t0 = time.perf_counter()
    tokens_before = sum(count_tokens(c.text) for c in chunks)
    try:
        kept, n_dedupe = dedupe(chunks, settings)
        kept, n_floor = relevance_floor(kept, settings)
        kept, n_sent = await token_budget_fill(query, kept, settings)
    except Exception as exc:            # noqa: BLE001 — compression is best-effort, never blocks
        logger.warning("rag.compression_failed", error=str(exc))
        return chunks
    tokens_after = sum(count_tokens(c.text) for c in kept)
    observability.log_compression(
        tokens_before=tokens_before, tokens_after=tokens_after,
        chunks_before=len(chunks), chunks_after=len(kept),
        sentences_dropped=n_sent, compression_ms=int((time.perf_counter() - t0) * 1000),
    )
    return kept


def build_document_compressor(settings):
    # API surface ONLY (AC / FR2), off the runtime path — mirrors rerank.build_compression_retriever.
    # DocumentCompressorPipeline([CrossEncoderReranker(model=<shared>, top_n=RERANK_TOP_N),
    #                             <F8 BaseDocumentCompressor filters>]) over the SAME loaded model.
    ...
```

```python
# app/rag/flags.py  — one added key (AC-17)
def apply_flags(settings, flags):
    return settings.model_copy(update={
        "ENABLE_HYBRID": flags.hybrid,
        "ENABLE_RERANK": flags.rerank,
        "ENABLE_QUERY_REWRITE": flags.query_rewrite,
        "ENABLE_COMPRESSION": flags.compression,   # F8 addition
    })
```

```python
# app/rag/observability.py  — additive metric (AC-12), mirrors log_rerank / log_rewrite
def log_compression(tokens_before, tokens_after, chunks_before, chunks_after,
                    sentences_dropped, compression_ms) -> None:
    logger.info("rag.compression", tokens_before=tokens_before, tokens_after=tokens_after,
                chunks_before=chunks_before, chunks_after=chunks_after,
                chunks_dropped=chunks_before - chunks_after, sentences_dropped=sentences_dropped,
                compression_ms=compression_ms)
```

```python
# app/rag/baseline.py  — flag-gated block in _pipeline_events, between the refusal gate and chain_input
if settings.ENABLE_COMPRESSION:
    scoring_query = rewrite_result.normalized if rewrite_result else query   # AC-9
    chunks = await compression.compress_chunks(scoring_query, chunks, settings)
```

The compression call sits **after** the `if refusal.pre_llm_gate(chunks, settings): … return` block
(so a refused query never compresses) and **before** `chain_input` is built, so the reassigned `chunks`
is the single list used by `format_context`, the `full_prompt` cost count, and `parse_citations`.

---

## 5. The compression pipeline (design intent)

Order is **dedupe → floor → budget** for a reason:

1. **Dedupe first** so the budget is not spent on two copies of the same overlapping fixed-window
   chunk (F2 emits overlapping chunks by design). Lower-scored duplicate is dropped (AC-4).
2. **Floor second** to drop chunks the cross-encoder scored below `COMPRESSION_SCORE_FLOOR` — filler
   that survived F6's top-5 truncation only because 5 slots were available. `MIN_CHUNKS` guards the
   refusal-free set from over-flooring (AC-2).
3. **Budget last** — greedy-fill the survivors in rerank order to `COMPRESSION_TOKEN_BUDGET`; the one
   chunk that overflows is sentence-trimmed so its *relevant* sentences still reach the LLM while its
   filler is dropped (AC-7). Sentence relevance is the **same** cross-encoder signal used for ranking,
   scored against the `normalized` query, so the trimmer keeps what the reranker would have valued.

Sentences kept by the trimmer are re-emitted in **document order** (not score order) so the passage
reads naturally and `extract_quote` still lifts a coherent verbatim quote. Only `chunk.text` changes;
`page_start`/`page_end` and every other citation field are copied through untouched (AC-10), so the
citation still resolves to the correct page even though the body is shorter.

---

## 6. Error handling

| Failure | Detection | Handling |
|---|---|---|
| Cross-encoder scoring raises (model load, forward pass) | exception inside `_trim_chunk` / `token_budget_fill` | caught in `compress_chunks` → log `compression_failed`, return **uncompressed** chunks, answer proceeds (AC-13) |
| Overflow chunk has one giant sentence > budget | `_trim_chunk` keeps top sentence anyway | keep the single highest-scored sentence (never emit an empty `text`); still counts toward MIN_CHUNKS |
| All chunks identical (extreme dedupe) | `dedupe` would drop below MIN_CHUNKS | dedupe stops at `COMPRESSION_MIN_CHUNKS` — never returns fewer (AC-5) |
| `rerank_score is None` (rerank off) | `relevance_floor` | floor is a no-op for that chunk (kept); dedupe + budget still run (AC-3) |
| Empty input (`chunks == []`) | guard at top of `compress_chunks` | return `[]` immediately (the refusal gate already handled the empty case upstream) |
| Whitespace-only chunk text | `_split_sentences` returns `[]` | chunk contributes 0 tokens; kept as-is or dropped by budget without crashing |

Compression adds **no** OpenAI call, so there is **no** new `estimate_cost` site; the cost win is
observed through the existing generation `log_llm_cost` whose `tokens_in` now reflects the compressed
context (AC-14). The `compression_failed` fallback guarantees a flaky cross-encoder can never take down
Q&A (US-6), exactly as F7's rewrite fallback protects the rewrite call.

---

## 7. New Settings keys (central `app.core.settings.Settings`)

```python
# --- Context compression (F8) ---
ENABLE_COMPRESSION: bool = False        # prod/request toggle; False ≡ f7-rewrite-after gen path (AC-16)
COMPRESSION_SCORE_FLOOR: float = 0.25   # drop reranked chunks below this calibrated score (AC-1)
COMPRESSION_MIN_CHUNKS: int = 2         # never leave a non-refused query with fewer chunks (AC-2)
COMPRESSION_TOKEN_BUDGET: int = 2200    # greedy-fill budget; overflow chunk is sentence-trimmed (AC-6/7)
COMPRESSION_DEDUPE_JACCARD: float = 0.7 # 5-gram Jaccard above this drops the lower-scored duplicate (AC-4)
COMPRESSION_DEDUPE_NGRAM: int = 5       # word-level n-gram size for the dedupe similarity (AC-4)
# RERANK_MODEL / RERANK_DEVICE / RERANK_APPLY_SIGMOID are reused for sentence scoring, NOT redefined.
```

`ENABLE_COMPRESSION` joins the feature-flag block alongside `ENABLE_HYBRID`/`ENABLE_RERANK`/
`ENABLE_QUERY_REWRITE`. All keys carry defaults so `Settings()` still boots with no new env for the
compression-off default. The floor (`0.25`) and budget (`2200`) defaults come straight from the feature
brief; the eval gate is where they are tuned if the ≥25%/≤0.02 targets are missed (AC-21).

---

## 8. Alembic migrations

**None.** F8 changes only in-memory, pre-generation orchestration:

- `RetrievedChunk` gains **no** field — trimming replaces `text` on a `model_copy`; it is transient
  (never persisted), so it is not a table.
- `AnswerResponse` gains **no** field (compression is invisible to the response contract; the token
  saving surfaces only as fewer generation input tokens + the `rag.compression` telemetry).
- `PipelineFlags.compression` already exists (contracts.py) — no field added.
- `eval_runs`/`eval_results` already exist (F12-owned); the gate persists through F4's writer.

Stated explicitly (same convention F3–F7 used); the acceptance task asserts `alembic` autogenerate is
empty (AC-20).

---

## 9. Toggle wiring & the eval-suite consequence (AC-16/AC-17/AC-21)

F5 introduced `rag.flags.apply_flags` at exactly two seams (`baseline._pipeline_events`,
`evals.retrieval.run_retrieval`); F6/F7 each extended it by one key. F8 extends it by one more
(`flags.compression -> ENABLE_COMPRESSION`) and reuses **both** call sites verbatim.
`PipelineFlags.compression` and `evals.flags.parse_flags` already accept the key, so `--flags
compression=on` needs **no** parser change.

Unlike F5/F6/F7, F8 makes **no** structural change to a retrieval seam — the one wiring change is the
flag-gated `compress_chunks` call inside `_pipeline_events`. This has a deliberate eval consequence:

- **Retrieval suite** (`evals.retrieval.run_retrieval`) drives the `retrieve` seam directly and never
  reaches generation, so it never runs compression — **hit@k / MRR are byte-for-byte
  `f7-rewrite-after`**. That is the correct, honest result: compression is post-retrieval and cannot
  change what was retrieved. The gate reports this as "retrieval unchanged."
- **RAGAS suite** (`evals.ragas_suite`) drives `answer()` → `_pipeline_events`, so with
  `compression=on` it generates over the compressed prompt — this is where **faithfulness** (target
  drop ≤0.02) and **context_precision** are measured.
- **Latency/cost suite** drives `astream()` → `_pipeline_events`, so the compressed prompt shortens the
  generation input; the **prompt-token reduction** (target ≥25%) is computed from the per-request
  `rag.compression` `tokens_before`/`tokens_after` records emitted during the gate run (the latency
  suite's SSE `cost_mean` counts output tokens only — see `evals/latency.py` — so the input-token
  saving is read from the compression telemetry, not the SSE stream).

`COMPRESSION_*` sizes are pure Settings values (no `PipelineFlags` field), matching how F5 handled
`RETRIEVAL_MODE`, F6 `RERANK_CANDIDATE_K`, and F7 `REWRITE_*`.

---

## 10. Honoring the Shared Context contracts & the F3/F5/F6/F7 seam

- **`RetrievedChunk`:** F8 populates no new field and mutates only `text` on a copy; `dense/sparse/
  fused/rerank` scores carry through untouched so a downstream reader (F9 cache, F13 logging) sees the
  same score shape. Citation metadata (`doc_id`/`title`/`section_heading`/`page_*`/`anchor`) is
  preserved on every trimmed chunk (AC-10).
- **The retrieval seam:** `rewrite.retrieve` / `retriever.retrieve` keep their signatures and behaviour
  — F8 consumes their output *after* the refusal gate and does not wrap or alter them. The "swap the
  retrieval step without touching prompt/parsing/streaming" property still holds; compression is a new,
  separable pre-generation stage.
- **`MemoryContext`:** consumed only indirectly — the scoring query comes from `last_rewrite()` (F7),
  which already threaded memory. F8 adds no memory plumbing; the non-citable-history rule is unaffected
  (compression touches only retrieved chunks, which remain the sole citation source).
- **`Citation`:** unchanged — because the compressed list drives both `format_context` and
  `parse_citations` (AC-11), every `[n]` still maps to a retrieved chunk, and `extract_quote` still
  yields a verbatim ≤25-word quote from the (possibly trimmed) stored text.
- **`StageEvent` / SSE contract:** unchanged — F8 adds **no** stage (compression is internal, between
  `searching` done and `generating` started), so `stage* → token* → citations → meta → done|error` is
  stable for F14/F17; `rag.compression` metrics are structlog/Langfuse telemetry, not SSE fields.
- **`AnswerResponse`:** unchanged (no migration); `pipeline_flags.compression` reflects the toggle.
- **Prompt rule:** unchanged — answer only from retrieved context, cite `[n]`, refuse on insufficient
  context, quotes ≤25 words. Compression removes filler *from the context block*; it never adds content
  and never changes the system prompt.
- **Cost rule:** F8 adds **no** OpenAI call; the saving flows through the existing central
  `estimate_cost`/`log_llm_cost` path (fewer generation input tokens, AC-14), plus the additive
  `rag.compression` token telemetry.
- **Async mandate:** the cross-encoder sentence scoring reuses F6's `anyio.to_thread.run_sync(model.
  score, …)` offload; tiktoken counting, Jaccard/n-gram math, dedupe, and greedy fill run inline as
  cheap pure-CPU; the `app/rag/` async grep-guard (in `tests/rag/test_generation.py`, which globs
  `app/rag/*.py`) automatically covers `compression.py` and stays green (AC-15).
- **Toggle rule:** `ENABLE_COMPRESSION` (config) + `PipelineFlags.compression` (request/eval) make F8
  fully A/B-able and instantly roll-back-able to the identical `f7-rewrite-after` generation path
  (AC-16/US-5).
