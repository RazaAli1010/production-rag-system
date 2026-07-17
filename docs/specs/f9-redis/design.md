# F9 — Semantic Cache (Redis + Postgres) · design.md

**Module:** `backend/app/caching/` · **Phase:** C · **Depends on:** F7, F12 · **Flag:** `ENABLE_CACHE`
· **Model:** none new · **Eval gate:** `f9-cache-after` vs `f8-compression-after`

---

## 1. Module layout

```
backend/app/caching/                   NEW package
├── __init__.py                        NEW  (empty)
├── keys.py                            NEW  normalize / sha256 / key-terms / jaccard  (pure CPU)
├── redis_hot.py                       NEW  redis.asyncio client, fail-open get/set/flush
├── store.py                           NEW  SemanticCache: matrix, lookup, write, evict, flush
│                                           + module-level lookup()/schedule_write() seam
└── run.py                             NEW  CLI: --flush | --delete-request-id <id>

backend/app/rag/
├── baseline.py                        CHANGED  cache seam spliced into _pipeline_events; hit replay
├── retriever.py                       CHANGED  embed_query(); query_vec threaded to by-vector search
├── rewrite.py                         CHANGED  retrieve(..., rr=None, query_vec=None) — no re-rewrite
├── flags.py                           CHANGED  + "ENABLE_CACHE": flags.cache
├── observability.py                   CHANGED  + log_cache()
└── (prompt/context/rerank/compression/citations/refusal/errors/events)   UNCHANGED

backend/app/core/settings.py           CHANGED  + "Semantic cache (F9)" block
backend/app/db/models/ops.py           CHANGED  + CacheEntry.query_hash, CacheEntry.request_id
backend/app/db/migrations/versions/
└── 0003_f9_cache_entry_keys.py        NEW      the only schema change

backend/app/evals/
├── flags.py                           CHANGED  parse_flags(spec, *, allow_cache=False)  (AC-32)
├── run.py                             CHANGED  allow_cache=(suites == ["latency"])      (AC-33)
└── latency.py                         CHANGED  skip_cache = not flags.cache; cache metrics

backend/tests/cache/                   NEW  conftest, test_keys, test_store, test_redis_hot,
                                            test_adversarial, test_run, test_acceptance,
                                            test_settings_schemas
backend/pyproject.toml                 CHANGED  + redis==5.2.1
.github/workflows/ci.yml               CHANGED  sync-twin grep glob += app/caching/*.py
backend/tests/rag/test_generation.py   CHANGED  same glob
```

## 2. Key design decision: where does the query vector come from?

The cache needs a vector for the normalized query. Today **no query vector exists anywhere in the
app** — `PineconeVectorStore.asimilarity_search_with_score` embeds internally and discards it, once
per namespace per fan-out query (up to 3 × 2 = 6 embeds per request), and hands back none of them.

| Option | Mechanism | Verdict |
|---|---|---|
| **A. Embed a second time, just for the cache** | `aembed_query` at the cache seam; leave retrieval untouched | ❌ Violates the brief's "single embed per request"; adds ~150ms to **every miss** — the majority path — to save nothing. The seam is on the critical path precisely because a miss must not be penalised. |
| **B. Intercept LangChain's internal embed** | subclass/patch `PineconeVectorStore` to stash the vector | ❌ Depends on library internals; the vector arrives *after* the retrieval we are trying to skip, which is backwards — a hit must never retrieve. |
| **C. Embed once at the seam, thread the vector into retrieval** ✅ | `retriever.embed_query()` at the cache seam; on a miss pass `query_vec` down to `asimilarity_search_by_vector_with_score` (`langchain_pinecone.vectorstores:617`) | ✅ **Chosen.** A hit embeds once and retrieves nothing. A miss embeds once and *removes* the 2 namespace-fan-out embeds for the normalized query — the miss path gets slightly cheaper, not more expensive. `query_vec=None` restores the exact current path (AC-13). |

The ordering consequence, and why the hot layer is checked before embedding at all:

```
Redis exact hit   →  no embed, no retrieve.      ~5–30ms      (the "instant hits" of the brief)
Semantic hit      →  1 embed + 1 matmul.         ~150–250ms   (the < 300ms acceptance target)
Miss              →  1 embed, reused downstream. ~= f8 latency, minus 2 redundant embeds
```

**Related decision — `app/caching/` not `app/rag/`.** CLAUDE.md fixes both the package path and the
`python -m app.caching.run --flush` CLI. The sync-twin grep guard currently globs `app/rag/*.py` only,
so it must grow the new glob or the async mandate silently stops covering the one module that talks
to Redis (AC-29).

**Related decision — lazy matrix load, not FastAPI startup.** The brief says "rebuilt from Postgres at
startup". `app/api/` does not exist yet (F11). A lazy first-use rebuild under an `asyncio.Lock`
(AC-22) satisfies the same requirement (no drift across restarts), works in-process for the F4
harness and the CLI, and needs no rework when F11 adds a lifespan hook — the hook will just call
`_ensure_loaded()` eagerly.

## 3. Data-flow diagram

```
_pipeline_events(query, k, ns, flags, memory, session, settings)
  │
  ├─ settings = flags.apply_flags(settings, flags)          # + ENABLE_CACHE <- flags.cache (AC-31)
  │
  ├─ if not settings.ENABLE_CACHE: ───────────────────────► f8-compression-after path, untouched
  │                                                          (no cache_lookup stage — AC-30)
  ├─ stage cache_lookup started                                                          (AC-23)
  │
  ├─ rr = await rewrite.rewrite_query(query, memory, settings)   if ENABLE_QUERY_REWRITE  (AC-12)
  ├─ normalized = keys.normalize(rr.normalized if rr else query)
  │
  ├─ redis_hot.get(keys.exact_key(normalized))              [async I/O, fail-open]  (AC-1/3/4)
  │     └─ hit + manifest ok ──────────────────────────────────────────┐            (AC-2)
  │
  ├─ vec = await retriever.embed_query(normalized, settings)  [1 embed, aembed_query] (AC-5)
  │
  ├─ store.lookup(normalized, vec)                                                    (AC-6..10)
  │     ├─ _ensure_loaded()      [awaited async DB read, once per process]            (AC-22)
  │     ├─ cosine = M @ v        [INLINE pure-CPU numpy matmul, ~10k×1536]            (AC-6)
  │     ├─ best < THRESHOLD                        ──► miss
  │     ├─ jaccard(terms(q), terms(hit.q)) < MIN   ──► miss + rag.cache_lexical_reject (AC-8)
  │     ├─ hit.index_manifest_id != current        ──► miss + DELETE entry (lazy)      (AC-9)
  │     └─ accept ─────────────────────────────────┐                                   (AC-7)
  │                                                │
  ├─ stage cache_lookup done (ms)                  │
  │                                                ▼
  │                                    ┌───────── HIT REPLAY ─────────┐               (AC-24/25)
  │                                    │ stage searching   skipped    │
  │                                    │ stage generating  skipped    │
  │                                    │ stage citing      skipped    │
  │                                    │ token   (full cached answer) │
  │                                    │ citations                    │
  │                                    │ meta    (cache_hit=true)     │
  │                                    │ done                         │
  │                                    │ log_cache(hit=True, ...)     │  (AC-26/27)
  │                                    │ hits++, last_hit_at=now()    │  write-behind (AC-7)
  │                                    └──────────────────────────────┘
  │  MISS
  ▼
  stage searching started
  chunks = await rewrite.retrieve(query, k, ns, settings, memory, rr=rr, query_vec=vec)  (AC-11/12)
  │        └─ normalized query: asimilarity_search_by_vector_with_score(vec, ...)  [no embed]
  │        └─ variants v1/v2:   asimilarity_search_with_score(q, ...)              [embed each]
  refusal gate → compress (F8) → generate → citing        [UNCHANGED f8 path]
  │
  └─ after `done`: if not refused and not degraded and citations:                        (AC-14/16)
        store.schedule_write(normalized, vec, response, ...)   # asyncio.create_task
              └─ Redis SETEX (TTL 24h)  +  Postgres upsert by query_hash  [OFF the response path]
                 evict least-recently-hit if count >= CACHE_MAX_ENTRIES                  (AC-18)
```

**Async-mandate placement (CLAUDE.md "which side of the line"):** Redis, Postgres and the embed call
are awaited async I/O. The numpy cosine matmul, sha256, `normalize`, and the Jaccard set math run
**inline as cheap pure-CPU** — CLAUDE.md names "cosine matmul on the cache matrix" explicitly as the
inline side of the line. A 10k×1536 `float32` matmul is ~15M FLOPs, sub-millisecond in BLAS; nothing
here goes to `anyio.to_thread`.

## 4. Key function signatures

```python
# app/caching/keys.py — pure CPU, no I/O, no settings
def normalize(query: str) -> str:
    """Lowercase, collapse whitespace, strip trailing punctuation. The ONE normalization both
    tiers key on, so the Redis key and the Postgres query_hash can never disagree."""

def exact_key(normalized: str) -> str:
    """`campusrag:cache:{sha256hex}` — the Redis key and the basis of `cache_entries.query_hash`."""

def key_terms(normalized: str) -> frozenset[str]:
    """Content words: tokens of len >= 3 minus a small stopword set, plus every numeric token
    (section ids like `15(3)` and degree levels like `bs`/`mphil` are exactly what the guard
    exists to distinguish, so short/numeric tokens are kept, not filtered)."""

def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    """|a ∩ b| / |a ∪ b|; 0.0 when both empty."""


# app/caching/redis_hot.py — every function is fail-open (AC-3/AC-4)
async def get(key: str, *, settings) -> dict | None:
    """None on miss, on any Redis error (logged `rag.cache_degraded`), or when REDIS_URL is None."""

async def set(key: str, payload: dict, *, settings) -> None: ...
async def flush(*, settings) -> int:
    """Delete every `campusrag:cache:*` key via async SCAN (never `KEYS`). Returns the count."""


# app/caching/store.py
class SemanticCache:
    """One instance per process. `_ids`/`_terms`/`_manifests` are row-parallel with `_matrix`'s
    rows and are mutated only under `_lock`."""
    _matrix: np.ndarray | None          # (n, 1536) float32, L2-normalized at load
    _lock: asyncio.Lock

    async def _ensure_loaded(self, *, settings, sessionmaker) -> None: ...          # AC-22
    async def lookup(self, normalized: str, vec: list[float], *, settings,
                     sessionmaker) -> tuple[AnswerResponse, float] | None: ...      # AC-6..10
    async def write(self, normalized: str, vec: list[float], response: AnswerResponse,
                    request_id: str, *, settings, sessionmaker) -> None: ...        # AC-17/18/19
    async def flush(self, *, settings, sessionmaker) -> int: ...                    # AC-20
    async def delete_by_request_id(self, request_id: str, *, sessionmaker) -> int:  # AC-21
        ...

def schedule_write(normalized, vec, response, request_id, *, settings, sessionmaker) -> None:
    """Fire-and-forget the write (AC-14/15). Holds the task in a module-level set and discards it
    on completion — an un-referenced task can be GC'd mid-await."""

async def lookup(normalized, vec, *, settings, sessionmaker): ...   # module-level seam over the singleton


# app/rag/retriever.py — NEW + CHANGED
async def embed_query(query: str, settings) -> list[float]:
    """The ONE query embed per request (AC-5). Reuses `_build_store`'s `OpenAIEmbeddings`
    (EMBED_MODEL / OPENAI_API_KEY) via `.aembed_query` — the async surface the grep guard allows."""

async def _retrieve_namespace(query, k, namespace, settings, query_vec=None): ...
async def dense_retrieve(query, k, namespace, settings, query_vec=None): ...
async def gather_candidate_pool(query, k, namespace, settings, query_vec=None): ...
async def retrieve(query, k, namespace, settings, query_vec=None): ...
# query_vec=None on every one => byte-for-byte the current path (AC-13).


# app/rag/rewrite.py — CHANGED
async def retrieve(query, k, namespace, settings, memory=None, rr=None, query_vec=None):
    """`rr` pre-computed by the F9 seam => `rewrite_query` is NOT called again (AC-12). `query_vec`
    is applied ONLY to the fan-out query equal to `rr.normalized`; variants embed themselves."""


# app/rag/observability.py — NEW
def log_cache(hit: bool, tier: str, cosine: float | None, jaccard: float | None,
              lookup_ms: int, n_entries: int, tokens_saved: int,
              est_cost_saved_usd: float) -> None:
    """AC-26. `est_cost_saved_usd` is computed by the CALLER via F2's central `estimate_cost`
    (AC-27) — mirrors log_rerank/log_rewrite/log_compression: a structlog emit, no rate table."""
```

## 5. The lookup, in design intent

The two-signal accept rule is the whole feature. Cosine ≥ 0.95 says *these questions are about the
same thing*; it does not say *these questions have the same answer*. "What is the BS admission
deadline?" and "What is the MPhil admission deadline?" are ~0.97 cosine under
`text-embedding-3-small` — near-identical syntax, one swapped noun — and have entirely different
answers with different citations. Serving one for the other is exactly the hallucination-shaped
failure this project refuses to ship.

The lexical guard catches it because the discriminating token (`bs` vs `mphil`) is a *content word*
that embedding similarity averages away but set overlap does not:

```
terms("what is the bs admission deadline")    = {what, bs, admission, deadline}
terms("what is the mphil admission deadline") = {what, mphil, admission, deadline}
jaccard = 3/5 = 0.60   ── clears 0.3, so cosine+lexical alone is NOT sufficient here
```

So the guard is calibrated at **T7 against the committed adversarial set**, not guessed: the shipped
`CACHE_SIMILARITY_THRESHOLD` / `CACHE_LEXICAL_JACCARD_MIN` pair must separate every adversarial pair
from every true-paraphrase pair, and T7's job is to find that pair of numbers and record them — the
same "tune, don't guess" discipline `REFUSAL_RERANK_THRESHOLD` got at the F6 gate. If no threshold
pair separates the two sets, the fallback is a **discriminative-term rule**: reject when either query
carries a `CACHE_DISCRIMINATIVE_TERMS` token (degree levels, `bs|ms|mphil|phd|adp`, and any numeric
section id) the other lacks. T7 documents which rule shipped and why. Being unable to defend the
threshold is a reason to ship the cache default-off, not a reason to loosen the threshold.

## 6. Error handling

| Failure | Detection | Handling |
|---|---|---|
| `REDIS_URL` unset | `settings.REDIS_URL is None` | Skip hot layer silently, no log spam (AC-4) |
| Redis down / timeout | `except Exception` + `asyncio.timeout(CACHE_REDIS_TIMEOUT_S)` | `rag.cache_degraded`, fall through to semantic layer (AC-3) |
| Redis payload unparseable | `ValidationError` on `AnswerResponse(**payload)` | Treat as miss, delete the key |
| Stale `index_manifest_id` | compare to `manifest.current_id(settings)` | Miss + delete the entry (AC-9) |
| Postgres lookup raises | `except Exception` around `lookup` | `rag.cache_degraded`, miss, answer normally (AC-10) |
| Cache write raises | `except Exception` inside the task body | `rag.cache_write_failed`, swallow (AC-19) |
| Matrix empty (cold cache) | `_matrix is None or len == 0` | Miss without a matmul |
| Unique-violation race on concurrent write | `ON CONFLICT (query_hash) DO UPDATE` | Upsert, no error (AC-17) |
| Matrix at capacity | `len(_ids) >= CACHE_MAX_ENTRIES` | Evict least-recently-hit, then insert (AC-18) |
| Task GC'd mid-write | strong ref in a module-level set | Never GC'd; discarded on done (AC-14) |

The invariant across every row: **the cache is an optimization, never a failure source.** There is no
cache error that produces a non-200 for the user.

## 7. New Settings keys (central `app.core.settings.Settings`)

```python
    # --- Semantic cache (F9) ---
    ENABLE_CACHE: bool = False  # prod/request toggle; False ≡ f8-compression-after path (AC-30)
    # Hot layer. None => Redis tier disabled entirely (AC-4) — the cache still works, Postgres-only,
    # so local dev and CI need no Redis. Upstash/`docker compose` URL in prod.
    REDIS_URL: RedisDsn | None = None
    CACHE_REDIS_TTL_S: int = 86_400  # 24h exact-match TTL (brief §1); Postgres tier has no TTL
    CACHE_REDIS_TIMEOUT_S: float = 0.25  # a slow hot layer must not out-cost the miss it saves (AC-3)
    CACHE_KEY_PREFIX: str = "campusrag:cache:"  # SCAN pattern for --flush (AC-20)
    # Accept rule (AC-7). BOTH must clear. Cosine alone collides near-identical-syntax/different-noun
    # pairs at ~0.97 ("BS admission deadline" vs "MPhil admission deadline") — design §5.
    # TUNED at T7 against tests/fixtures/cache/adversarial.jsonl, not guessed.
    CACHE_SIMILARITY_THRESHOLD: float = 0.95
    CACHE_LEXICAL_JACCARD_MIN: float = 0.3
    # Fallback guard if T7 finds no separating threshold pair (design §5): reject a match when either
    # query carries one of these discriminating tokens the other lacks.
    CACHE_DISCRIMINATIVE_TERMS: list[str] = ["bs", "ms", "mphil", "phd", "adp", "bsc", "msc"]
    # Brute-force ceiling: 10k × 1536 float32 = 61 MB resident, sub-ms matmul — the justification for
    # having no vector index (brief §1). At the cap, writes evict least-recently-hit (AC-18).
    # ponytail: single-process matrix; if the API ever runs >1 replica, revisit (requirements §5).
    CACHE_MAX_ENTRIES: int = 10_000
    # EMBED_MODEL / EMBED_DIM (F2) are reused for the query vector — NOT redefined.
    # RETRIEVAL_* / RERANK_* / COMPRESSION_* are untouched by the cache.
```

`RedisDsn` is imported from `pydantic` alongside the existing `EmailStr`/`PostgresDsn`/`SecretStr`.

## 8. Alembic migrations

**One:** `0003_f9_cache_entry_keys.py` (`down_revision = "0002"`).

F12's `0001_initial.py` already created `cache_entries` with `id`, `query_text`, `embedding` (BYTEA),
`answer` (JSONB), `index_manifest_id`, `hits`, `created_at`, `last_hit_at` — F9 does **not** recreate
any of that, and `embedding` stays `LargeBinary` (no pgvector). Two columns are genuinely missing:

- **`query_hash`** (`String`, NOT NULL, **unique** `uq_cache_entries_query_hash`) — without it the
  write path has no upsert key and re-asking a cached question inserts a duplicate row every time,
  growing the matrix without bound (AC-17). Not derivable from `query_text` in SQL cheaply enough to
  index.
- **`request_id`** (`String`, nullable, indexed `ix_cache_entries_request_id`) — poison control by
  request id (AC-21, brief §4). Nullable because `--flush`-era and CLI-seeded rows have none.

```python
def upgrade() -> None:
    op.add_column("cache_entries", sa.Column("query_hash", sa.String(), nullable=False,
                                             server_default=""))
    op.alter_column("cache_entries", "query_hash", server_default=None)
    op.create_unique_constraint("uq_cache_entries_query_hash", "cache_entries", ["query_hash"])
    op.add_column("cache_entries", sa.Column("request_id", sa.String(), nullable=True))
    op.create_index("ix_cache_entries_request_id", "cache_entries", ["request_id"], unique=False)
```

The `server_default=""` + drop dance is only for the (empty in practice) existing-rows case; names are
spelled out to match `base.py`'s naming convention so a post-upgrade `--autogenerate` diff is empty
(the rule `0001_initial.py`'s header states). `downgrade()` drops both, symmetric. Eval tables need no
change — F9 writes no new table, same as F4.

## 9. Toggle wiring & the eval-suite consequence (AC-30..AC-33)

`ENABLE_CACHE=false` must be byte-for-byte `f8-compression-after`, and it is, because every seam is
gated on it: no `cache_lookup` stage, `query_vec` stays `None` so retrieval takes today's by-query
path, and `rr` stays `None` so `rewrite.retrieve` runs its own rewrite exactly as it does now.

The eval harness needed a real decision. `evals/flags.py:40` currently does:

```python
values["cache"] = False  # AC-27: never let the harness measure a cache-hit path
```

That rule exists for a good reason (CLAUDE.md: "the F4 harness always runs `skip_cache=true` … **so
retrieval metrics stay comparable across labels**") and it is exactly right for retrieval, RAGAS and
refusal — a cached answer would make hit@k meaningless. But taken literally it also makes the F9 gate
impossible: F9's gate **is** latency/cost on a repeat-heavy workload, and CLAUDE.md itself scopes F9's
gate to "latency/cost suites only". So the rule is narrowed to exactly its rationale:

```python
def parse_flags(spec: str | None, *, allow_cache: bool = False) -> PipelineFlags:
    ...
    if not allow_cache:
        values["cache"] = False   # retrieval/RAGAS/refusal can never see a cache hit (AC-32)
    return PipelineFlags(**values)

# run.py
flags = parse_flags(args.flags, allow_cache=(expand_suites(args.suite) == ["latency"]))
```

`--suite all` still forces `cache=False` — the default and the safe path are unchanged. Only an
explicit `--suite latency --flags cache=on` can turn it on, which is precisely T13's gate command.
`latency.py:121`'s hardcoded `"skip_cache": True` becomes `not flags.cache` (AC-33).

**The repeat-heavy workload is already there.** `run_latency` samples
`answerable[i % len(answerable)]` for `EVAL_LATENCY_REQUESTS` requests — deterministic, identical
across labels, and with N > len(answerable) every question after the first pass is an exact repeat.
That IS the repeat workload the brief asks for; no new setting. The first pass populates, the rest
hit, and the hit rate is a known function of N — which is why T13 must run **both** labels at the
same N (f8's report was trimmed to 30 requests; match it).

**Metric naming is load-bearing** for `compare.py`'s direction arrows, which key off prefixes
(`_LOWER_IS_BETTER_PREFIXES = ("latency_", "cost_", "tokens_mean", "false_refusal_rate")`):

| metric | prefix effect | intent |
|---|---|---|
| `cache_hit_rate` | no prefix match → higher-is-better ▲ | ✅ correct |
| `cache_cost_saved_mean` | **not** `cost_` → higher-is-better ▲ | ✅ correct — do NOT name it `cost_saved_mean`, that would render ▼ on an improvement |
| `latency_cache_hit_p50` / `_p95` | `latency_` → lower-is-better ▲ on decrease | ✅ correct — do NOT name it `cache_hit_latency_*` |
| `latency_cache_lookup_p50` | harvested from the `cache_lookup` stage event by the existing per-stage parser | ✅ free, no code |

So **`compare.py` needs no change** — the naming does the work. That is the reason for these exact
names.

## 10. Honoring the Shared Context contracts & the F3/F5/F6/F7 seam

- **`AnswerResponse`** — unchanged. `cache_hit: bool = False` already exists (`contracts.py:125`); F9
  is the first writer of `True`. The two hardcoded `cache_hit=False` construction sites in
  `baseline.py` (157 refusal, 224 normal) stay `False` — a refusal is never cached (AC-16) and a
  freshly generated answer is by definition not a hit. The hit path constructs its own from the
  cached JSONB.
- **`PipelineFlags`** — unchanged. `cache: bool = False` already exists (`contracts.py:102`); F9 wires
  it through `apply_flags`, the same one-line pattern F5–F8 each added.
- **`StageEvent`** — unchanged. `contracts.py:93` types `stage` as a free-form `str`, and
  `cache_lookup` is already in CLAUDE.md's vocabulary. `status` stays inside the closed
  `Literal["started","done","skipped"]`.
- **SSE contract** — unchanged, no new event type. The hit path reuses the refusal path's exact shape
  (skipped stages → `citations` → `meta` → `done`), so F14 renders a hit with zero frontend change and
  the latency suite's stage parser reads it for free.
- **`Citation`** — unchanged; citations round-trip through `cache_entries.answer` JSONB and are
  re-validated by `AnswerResponse` on read. A cached citation still points at a retrieved chunk, never
  at history — the non-citable-history rule is untouched.
- **`Chunk` / `RetrievedChunk`** — never cached. The cache stores the *answer*, not the context; this
  is why an index change only needs `index_manifest_id`, not a chunk-level dependency graph.
- **F3 retriever seam** — preserved. `retrieve(query, k, namespace, settings)` grows one optional
  keyword-only-by-convention `query_vec=None` param; the shape F5/F6/F7 all swapped bodies of but
  never reshaped is intact, and `None` is the existing behaviour.
- **F5 hybrid / F6 rerank / F8 compression** — untouched. The cache sits entirely *before* retrieval
  (hit) or contributes only a vector *into* it (miss). `hybrid.was_degraded()` still governs
  `degraded`, which is one of the reasons an answer is not cached (AC-16).
- **F7** — the reason F9 is cheap. `rewrite.py`'s docstring already declares the decomposition
  "so F9 (semantic cache) can later rewrite-then-lookup without a double rewrite"; F9 collects on that
  by threading `rr` into `rewrite.retrieve`. The cache key is F7's **standalone** normalized question,
  so a follow-up ("aur MPhil ka?") is keyed on what it actually means and can never poison the cache
  with conversation-dependent text — exactly the CLAUDE.md session-memory rule.
- **F17 / F13 forward compatibility** — the cache is global and keyed on the standalone question, so
  session memory needs no cache change. `log_cache` mirrors `log_rerank`/`log_rewrite`/
  `log_compression`, so F13 routes it into `request_logs`/Langfuse without an F9 change.
- **Cost rule** — `estimate_cost()` (F2, `app/indexing/cost.py`) is reused verbatim; F9 adds no rate
  table (AC-27).
- **Privacy rule** — `cache_entries.query_text` stores the normalized query in the clear. This is
  consistent with the existing split: `request_logs.query_hash` is hashed telemetry;
  `cache_entries.query_text` is operational data the lexical guard must read at match time (and
  `--flush`/poison control must be inspectable). The privacy test covers `request_logs`, which F9 does
  not change.
