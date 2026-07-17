# F9 — Semantic Cache (Redis + Postgres) · requirements.md

**Module:** `backend/app/caching/` · **Phase:** C (production layer) · **Depends on:** F7 (normalized
query), F12 (`cache_entries`) · **Flag:** `ENABLE_CACHE` (default `false`) · **Model:** none new —
reuses `text-embedding-3-small` · **Eval gate:** `f9-cache-after` vs `f8-compression-after`
(latency/cost suites only)

---

## 1. Overview

Students ask the same questions in waves — probation rules in week 1, fee deadlines before semester
start, plagiarism policy at thesis time. Those waves are near-duplicates, not exact duplicates: the
same question arrives as "probation se kaise nikalta hoon", "how to get off probation", and "cgpa
prob rules". F9 serves the second and third from cache.

The mechanism:

1. **Redis hot layer** — exact-match `sha256(normalized_query)` → cached `AnswerResponse`, TTL 24h.
   Checked FIRST, before any embedding call, so an exact repeat costs one Redis round-trip.
2. **Postgres semantic layer** — on a Redis miss, embed the normalized query once, cosine it against
   an in-memory `float32` matrix of every live `cache_entries.embedding`, and accept the best match
   only if it clears BOTH a cosine threshold (`0.95`) AND a lexical key-term Jaccard guard (`0.3`).
3. **Miss** — the vector just computed is handed down into dense retrieval instead of being thrown
   away, so a miss costs the same number of embed calls as today's pipeline, not one more.
4. **Write-behind** — successful, non-refused, non-degraded answers are written to Redis + Postgres
   via `asyncio.create_task` AFTER the SSE stream has terminated. A cache write never adds latency.
5. **Invalidation** — every entry carries `index_manifest_id`; a re-index makes stale entries fail
   the manifest check lazily at lookup. `--flush` and per-query delete cover the rest.

### 1.1 Design decisions resolved in the feature brief (do NOT re-derive)

- **Two tiers, both real.** Redis is the exact-match hot layer; Postgres `cache_entries` is the
  semantic + durable layer. The dir is named `f9-redis`; the schema docstring in
  `app/db/models/ops.py` says Postgres+matmul. Both are correct — they are different tiers.
- **Brute-force cosine, no vector index.** `cache_entries` stays `< 10k` rows; a `10_000 × 1536`
  `float32` matrix is 61 MB and a single matmul is sub-millisecond. Pinecone is the vector store;
  the cache embedding is compared in-process only. pgvector remains banned.
- **Embedding is BYTEA**, `float32[1536]`, already migrated by F12.
- **A second, non-embedding signal is not optional.** Cosine alone cannot separate paraphrases from
  near-duplicates that differ by one identifier — measured at T7, the sets overlap. (The specced
  form of that signal, a Jaccard floor, did not survive contact with the data; the shipped form is
  a discriminative-token veto. See design §5.)
- **Cache is an optimization, never a failure source.** Every Redis and Postgres cache error is
  caught and logged; the request proceeds as an uncached request.

## 2. User stories

**US-1 (Student):** As a student asking a question three other students already asked this morning,
I want an instant answer, so I am not waiting ~4s on mobile data for a result the system already has.

**US-2 (Student):** As a student who phrases the question differently from whoever asked it first, I
want my paraphrase to hit the same cached answer, so cache value is not limited to byte-identical
repeats.

**US-3 (Student):** As a student asking about **MPhil** deadlines, I want to NOT receive the cached
**BS** deadline answer, so a near-miss never becomes a wrong answer with real citations attached.

**US-4 (Ops / cost owner):** As the person paying the OpenAI bill, I want hit rate, tokens saved and
dollars saved recorded per request, so the cache's value is measured and not assumed.

**US-5 (Ops):** As an operator, I want the cache to be switchable off in prod without a deploy, so a
poisoned or misbehaving cache is one flag away from bypassed.

**US-6 (Ops):** As an operator whose Redis (Upstash free tier) just went down, I want the pipeline to
keep answering, so a cache outage is a latency event, not an availability event.

**US-7 (Ops):** As an operator who just re-indexed the corpus, I want answers citing the old index to
stop being served, so the cache cannot outlive the documents it quoted.

**US-8 (Ops):** As an operator who found one bad cached answer, I want to delete that single entry by
the question that produced it, so poison control does not require flushing the whole cache.

**US-9 (Eval owner):** As the eval owner, I want the F4 harness's retrieval/RAGAS/refusal suites to
be structurally incapable of measuring a cache hit, so hit@k and faithfulness stay comparable across
every label.

**US-10 (Eval owner):** As the eval owner, I want the latency suite to be ABLE to measure the cache
path, so the F9 gate can prove the latency/cost win it exists to prove.

## 3. EARS acceptance criteria

### 3.1 Lookup — the hot layer

- **AC-1 (Ubiquitous — exact key):** When the cache is on, the system shall compute
  `sha256(normalize(q))` — where `q` is the F7 `normalized` query when rewrite ran, else the raw
  query — and query Redis for that key BEFORE computing any embedding.
- **AC-2 (Event-driven — hot hit):** When the Redis key exists and its stored `index_manifest_id`
  equals the current manifest id, the system shall replay the cached `AnswerResponse` with
  `cache_hit=true` and shall not embed, retrieve, rerank, compress or call the generation LLM.
- **AC-3 (Unwanted — Redis unavailable):** If any Redis operation raises or exceeds
  `CACHE_REDIS_TIMEOUT_S`, the system shall log `rag.cache_degraded`, skip the hot layer for that
  request, and continue to the semantic layer — the request shall not fail.
- **AC-4 (State-driven — Redis unconfigured):** While `REDIS_URL` is `None`, the system shall skip
  the hot layer entirely without logging a degradation on every request.

### 3.2 Lookup — the semantic layer

- **AC-5 (Ubiquitous — single embed):** On a hot-layer miss with the cache on, the system shall embed
  the normalized query EXACTLY once per request via `aembed_query`, and shall pass that same vector
  into dense retrieval on a cache miss (AC-11) — total embed calls for the normalized query shall not
  increase versus `f8-compression-after`.
- **AC-6 (Ubiquitous — cosine):** The system shall score the query vector against every live cached
  embedding by cosine similarity computed inline as a single numpy matmul over the L2-normalized
  matrix.
- **AC-7 (Event-driven — semantic hit):** When the best-scoring entry's cosine is
  `>= CACHE_SIMILARITY_THRESHOLD` (calibrated `0.86`) **AND** the two normalized queries carry the
  same discriminator set (`keys.discriminators`: `CACHE_DISCRIMINATIVE_TERMS` ∪ years ∪ section
  ids), the system shall return that entry's `AnswerResponse` with `cache_hit=true`, increment
  `hits`, and set `last_hit_at`.
- **AC-8 (Unwanted — discriminator disagreement):** If the cosine clears the threshold but the two
  queries disagree on any discriminator, the system shall treat the request as a MISS and log
  `rag.cache_lexical_reject` with the cosine and both discriminator sets.

  > **Amended after T7's calibration.** These ACs originally specified `cosine >= 0.95 AND
  > key-term Jaccard >= 0.3`. Measured against real `text-embedding-3-small` vectors, both numbers
  > were wrong: nothing reaches 0.95 (the tier would never fire), and the Jaccard floor is inert
  > once the veto exists (optimal value 0.0 — adversarial pairs score *higher* Jaccard than real
  > paraphrases). The two sets also overlap on cosine, so no threshold alone can separate them.
  > `CACHE_LEXICAL_JACCARD_MIN` is deleted. Full table in design §5; evidence in
  > `tests/cache/test_adversarial.py`.
- **AC-9 (Unwanted — stale manifest):** If the best match's `index_manifest_id` differs from the
  current manifest id, the system shall treat the request as a miss and shall delete that entry from
  Postgres and from the in-memory matrix (lazy expiry, US-7).
- **AC-10 (Unwanted — cache backend error):** If the Postgres lookup or matrix access raises, the
  system shall log `rag.cache_degraded`, treat the request as a miss, and answer normally.

### 3.3 Miss path & vector reuse

- **AC-11 (Event-driven — vector threading):** On a cache miss, the system shall pass the computed
  query vector to `retriever.dense_retrieve` for the normalized query, which shall call
  `asimilarity_search_by_vector_with_score` instead of `asimilarity_search_with_score`. F7 paraphrase
  variants shall continue to embed themselves.
- **AC-12 (Ubiquitous — no double rewrite):** When both `ENABLE_CACHE` and `ENABLE_QUERY_REWRITE` are
  on, the system shall call `rewrite.rewrite_query` exactly ONCE per request and thread the resulting
  `RewriteResult` into retrieval — the pipeline shall not pay for two rewrite LLM calls.
- **AC-13 (State-driven — vector absent):** While `query_vec` is `None` (cache off), every retrieval
  function shall behave byte-for-byte as `f8-compression-after`.

### 3.4 Write

- **AC-14 (Event-driven — write-behind):** When a request produced a non-refused, non-degraded
  answer with at least one citation, the system shall schedule the Redis and Postgres writes via
  `asyncio.create_task` AFTER the terminal `done` event, holding a strong reference to the task so it
  is not garbage-collected mid-flight.
- **AC-15 (Ubiquitous — write latency):** The cache write shall not be awaited on the response path —
  the `done` event's timestamp shall not depend on the write completing.
- **AC-16 (Unwanted — never cache a refusal):** If `refused` is `true`, or `degraded` is `true`, or
  `citations` is empty, or the answer came from the cache, the system shall not write an entry.
- **AC-17 (Ubiquitous — upsert):** The system shall key the Postgres row on
  `query_hash = sha256(normalize(normalized_query))` with a unique constraint, and re-writing the
  same normalized query shall update the existing row rather than create a duplicate.
- **AC-17b (Ubiquitous — cached token counts):** The system shall persist the `tokens_in` and
  `tokens_out` the cached answer originally cost, so a later hit can report what it saved (AC-27).
  These ride on `AnswerResponse` itself (AC-27b), so they are carried by the existing `answer` JSONB
  with no extra column.
- **AC-18 (Unwanted — capacity):** If the live entry count is `>= CACHE_MAX_ENTRIES` (default
  `10_000`), the system shall evict the least-recently-hit entry before inserting, so the brute-force
  matrix stays inside its justified size.
- **AC-19 (Unwanted — write failure):** If a cache write raises, the system shall log
  `rag.cache_write_failed` and swallow the error — a write failure shall never surface to the client
  or crash the event loop.

### 3.5 Invalidation & operations

- **AC-20 (Ubiquitous — CLI flush):** `python -m app.caching.run --flush` shall delete every
  `cache_entries` row and every Redis cache key, print the deleted count, and exit `0`.
- **AC-21 (Ubiquitous — poison control):** `python -m app.caching.run --delete-query "<question>"`
  shall normalize the question to its `query_hash`, delete the single entry matching it from Postgres
  and Redis, and exit `0` (exit `1` when no row matched).

  > **Deviation from the brief (§4 "per-entry delete by request_id"), recorded deliberately.** No
  > `request_id` is generated anywhere in the pipeline yet — F13 owns request logging — so a
  > `request_id` column would be `NULL` on every row F9 writes and `--delete-request-id` would match
  > nothing. Deleting by query text keys on the `query_hash` column F9 needs anyway (AC-17), needs no
  > extra schema, and matches the operator's real workflow: they see a bad answer, and what they have
  > in hand is the question. F13 may add `request_id` when it has one to write.
- **AC-22 (Ubiquitous — lazy matrix load):** The system shall rebuild the in-memory matrix from
  Postgres on first cache use in the process, guarded by an `asyncio.Lock` so concurrent first
  requests rebuild it once, and shall re-derive it from Postgres after every process restart (no
  matrix/Postgres drift).

### 3.6 SSE contract & response shape

- **AC-23 (Ubiquitous — stage events):** The cache lookup shall emit paired
  `stage cache_lookup started` / `done` events with `ms` populated on `done`.
- **AC-24 (Event-driven — hit stream shape):** On a cache hit the system shall emit
  `stage cache_lookup started/done` → `stage searching skipped` → `stage generating skipped` →
  `stage citing skipped` → ONE `token` event carrying the full cached answer text → `citations` →
  `meta` (`cache_hit=true`) → `done`, preserving the ordered SSE contract with no new event type.
- **AC-25 (Ubiquitous — replay fidelity):** The text reassembled from `token` events on a cache hit
  shall equal the `answer` of the originally cached `AnswerResponse` byte-for-byte, including the
  disclaimer suffix — `answer()` and `astream()` shall agree on a hit exactly as they do on a miss.

### 3.7 Metrics & cost

- **AC-26 (Ubiquitous — cache metrics):** Every cache lookup shall emit `rag.cache` via
  `observability.log_cache` carrying `hit`, `tier` (`redis` | `semantic` | `miss`), `cosine`,
  `jaccard`, `lookup_ms`, `n_entries`, `tokens_saved` and `est_cost_saved_usd`.
- **AC-27 (Ubiquitous — central cost helper):** Dollars saved shall be computed with F2's central
  `estimate_cost(model, tokens_in, tokens_out)` over the cached response's `tokens_in`/`tokens_out` —
  the tokens the hit avoided spending — with no second rate table anywhere in `app/caching/`.
- **AC-27b (Ubiquitous — restore the AnswerResponse token fields):** `AnswerResponse` shall carry
  `tokens_in: int = 0` and `tokens_out: int = 0`, populated at every construction site in
  `_pipeline_events`.

  > **Why this is F9's job.** The Shared Context canonical contract specifies `tokens_in`/`tokens_out`
  > on `AnswerResponse`; the implemented model omits them — pre-existing F3 drift.
  > `_pipeline_events` already computes both as locals and discards them. F9 is the first feature that
  > needs them (a hit must report the tokens it avoided), so F9 restores them. The change is additive
  > with defaults: no existing test breaks, and `AnswerResponse` is not a table, so no migration. The
  > payoff is that `$ saved` becomes computable AND rides out on the SSE `meta` event, which is what
  > lets the F4 latency suite emit `cache_cost_saved_mean` with no extra plumbing (AC-33b).
  >
  > `request_id` and `latency_ms` — also in the canonical contract, also missing — are deliberately
  > NOT added here: F9 has no use for them and F13 owns request logging. Restoring a contract field
  > with no consumer is speculative.
- **AC-28 (Ubiquitous — request log fields):** `RequestLog.cache_hit` and `RequestLog.embed_ms`
  (already migrated by F12) shall be populated from this path; no metric named in this spec is
  emitted without a log site.

### 3.8 Async mandate, toggling, settings, migration, gate

- **AC-29 (Ubiquitous — async surface):** All cache I/O shall be async: `redis.asyncio` (the sync
  client is banned), async SQLAlchemy for `cache_entries`, `aembed_query` for the embedding. The
  cosine matmul, sha256 hashing and Jaccard computation are cheap pure-CPU and shall run inline.
- **AC-29b (Ubiquitous — CI guard):** `.github/workflows/ci.yml` shall gain a `caching:` job carrying
  its own async-guard block over `app/caching` (banning `.embed_query(`/`.embed_documents(`,
  `import requests`, bare `import redis` / `from redis import …`, `.invoke(`, `create_engine(`) plus
  `ruff check app/caching`, and `backend/tests/cache/test_no_sync_calls.py` shall enforce the same in
  the suite.

  > CI gives each package its own guard block (`app/db`, `app/ingestion`, `app/indexing`, `app/rag`,
  > `app/evals`) rather than one shared glob, so a new package needs a new job — there is no glob to
  > widen. Without this, `app/caching` would be the one module talking to Redis and *not* covered by
  > the async mandate.
- **AC-30 (State-driven — prod/request toggle):** While `ENABLE_CACHE` is `false` (the default),
  `_pipeline_events` shall skip lookup and write entirely and behave byte-for-byte as
  `f8-compression-after`, emitting no `cache_lookup` stage.
- **AC-31 (State-driven — request flag):** `PipelineFlags.cache` (already declared) shall map onto
  `ENABLE_CACHE` through `rag/flags.apply_flags`; `flags.cache=false` IS the `skip_cache` bypass
  until F11 adds the HTTP request field, which will map `skip_cache=true` → `flags.cache=false`.
- **AC-32 (State-driven — eval isolation):** While the requested suites are anything other than
  latency-only, `evals.flags.parse_flags` shall force `cache=False` regardless of the `--flags`
  string, so retrieval/RAGAS/refusal metrics can never measure a cache-hit path.
- **AC-33 (Event-driven — eval cache measurement):** When the run is `--suite latency` alone,
  `parse_flags` shall honour an explicit `cache=on`, and `run_latency` shall post
  `skip_cache = not flags.cache` rather than a hardcoded `True`.
- **AC-33b (Ubiquitous — cache eval metrics):** The latency suite shall emit `cache_hit_rate`,
  `cache_cost_saved_mean`, and `latency_cache_hit_p50`/`_p95` (percentiles over hit requests only),
  read off the SSE `meta` event. It shall NOT alter `cost_mean` or `tokens_mean`: both are recorded
  at `f8-compression-after` on a fixed basis (`cost_mean` is output-only; `tokens_mean` counts
  `token` events), and changing either would make the gate's delta table compare two different
  measurements. These names are chosen so `compare.py`'s prefix-driven direction arrows are correct
  without a `compare.py` change (design §9).
- **AC-34 (Ubiquitous — Settings centralisation):** Every new config value (`ENABLE_CACHE`,
  `REDIS_URL`, `CACHE_*`) shall live in the single `app.core.settings.Settings` class with no module
  reading `os.environ` directly.
- **AC-35 (Ubiquitous — migration):** The single new `cache_entries` column (`query_hash`, NOT NULL,
  unique) shall be added by Alembic revision `0003`, with a symmetric `downgrade()` and an
  autogenerate-clean diff afterwards. F12's `0001_initial.py` already created the rest of the table;
  `embedding` stays `LargeBinary` (no pgvector). No other schema change is in F9's scope.
- **AC-36 (Ubiquitous — eval gate):** F9 shall not be done until
  `docs/eval_results/f9-cache-after.md` and
  `docs/eval_results/f9-cache-after-vs-f8-compression-after.md` are committed, mapping the label to a
  git SHA + index manifest.

## 4. Acceptance criteria (feature-level definition of done)

1. A paraphrase of a cached question hits in **< 300ms end-to-end** (`latency_cache_hit_p95`), and an
   exact repeat hits without any embed call.

   > **Measured at T15 — this target is configuration-dependent and NOT met in every config.** The
   > cache key is F7's normalized question, so the rewrite must run *before* the lookup (CLAUDE.md's
   > pipeline order). Live numbers, Postgres-only tier:
   >
   > | config | `cache_lookup` stage | end-to-end hit |
   > |---|---|---|
   > | rewrite ON | **7,625ms** (gpt-4o-mini rewrite alone = 5,065ms) | 3,781ms |
   > | rewrite OFF (F7's shipped default) | **1,703ms** (embed-bound, cold client) | — |
   > | rewrite OFF + Redis exact hit | *not measured — no Redis on the dev box* | — |
   >
   > Only the Redis exact-match tier can plausibly clear 300ms: it is the one path that skips both
   > the rewrite and the embed. With rewrite ON the target is **unachievable by construction** — a
   > 5s LLM call precedes every lookup. The AC stands as written for the Redis exact path; the
   > semantic path's honest number is embed-bound. Verified speed-up end-to-end: **18×**
   > (69,188ms → 3,781ms), byte-identical replay.
2. The committed adversarial pair set ("BS admission deadline" vs "MPhil admission deadline", and
   siblings) does **NOT** collide at the shipped `CACHE_SIMILARITY_THRESHOLD` /
   `CACHE_LEXICAL_JACCARD_MIN` — test committed under `backend/tests/cache/`.
3. Hit rate, tokens saved and $ saved are logged on every lookup and surface as
   `cache_hit_rate` / `cache_cost_saved_mean` eval metrics (F13 later routes them to the stats
   endpoint without an F9 change).
4. Redis down → answers still served, `rag.cache_degraded` logged, no 5xx.
5. Re-index (new `index_manifest_id`) → stale entries are not served and are deleted on lookup.
6. `--flush` and `--delete-query` both work against a live Postgres + Redis.
7. `ENABLE_CACHE=false` is byte-for-byte `f8-compression-after` — proved by a toggle-parity test.
8. Retrieval/RAGAS/refusal eval runs show **0% cache interference** (`parse_flags` forces
   `cache=False`); a latency-only run can turn it on.
9. Alembic `0003` applies and downgrades cleanly; `alembic revision --autogenerate` produces an
   empty diff afterwards.
10. The `caching:` CI job's async guard passes over `app/caching`.
11. **Eval gate:** `f9-cache-after` vs `f8-compression-after` delta report committed (see tasks T13).

## 5. Out of scope (do not implement here)

- **The `/api/ask` HTTP `skip_cache` request field** — F11 owns the request model. F9 ships the
  in-process bypass (`flags.cache`) that F11 will map onto.
- **The stats endpoint** — F13 owns it. F9 only guarantees every number it needs is logged.
- **`request_logs` row writing** — F13 owns the writer. F9 populates the existing `cache_hit` /
  `embed_ms` fields through its log records.
- **`AnswerResponse.request_id` / `.latency_ms`** — both are in the canonical Shared Context contract
  and both are missing from the implementation, but F9 has no consumer for either and F13 owns
  request identity. F9 restores only the two fields it actually uses (AC-27b).
- **A `request_id` column on `cache_entries`** — see AC-21. Nothing can populate it until F13.
- **pgvector / any DB-side vector index** — fixed stack decision; brute-force matmul under 10k rows.
- **Redis rate limiting** — F11.
- **Caching refusals, degraded answers, or per-user/per-session cache partitioning** — the cache is
  global and stores only clean, cited answers. Session-scoped follow-ups are already condensed to a
  standalone question by F7, which is why the cache key cannot be poisoned by conversation state.
- **Distributed cache-matrix invalidation across API replicas** — single-process matrix, rebuilt at
  startup; a second replica may serve a stale-but-manifest-valid entry for up to its own TTL. Revisit
  if the API ever runs > 1 replica.
