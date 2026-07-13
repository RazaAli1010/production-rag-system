# F2 — Chunking & Indexing · requirements.md

**Module:** `backend/app/indexing/`
**Phase:** A (foundation) · **Depends on:** F1 (Ingestion) · **Blocks:** F3 (Baseline RAG)
**Status of eval gate:** N/A — F2 is a Phase A foundation feature, not a Phase B/C enhancement,
so no F4 eval-gate task applies. The eval-gate sequence begins at F5.

---

## 1. Overview

F2 turns F1's **clean, citation-anchored `Document` blocks** (`data/extracted/{doc_id}.jsonl`)
into a fully searchable index. It splits each document into `Chunk`s with one of two comparable
LangChain strategies, embeds them via `langchain-openai` (`text-embedding-3-small`, 1536-dim),
upserts the vectors to Pinecone serverless (namespace = `source_org`), builds an in-process BM25
corpus persisted to `data/bm25.pkl`, and writes the `Chunk` rows to Postgres so that citations
and evals never round-trip to Pinecone for metadata.

Chunking is the single most consequential retrieval-quality lever, so F2 ships **two strategies
behind a config flag** (`fixed` vs `structure`) to make the choice measurable downstream. A
strategy change is destructive: mixed chunk populations are forbidden, so switching strategy
forces a `--wipe` and re-index.

The unit of durable output F2 owns is: Pinecone vectors (one per chunk), `chunks` rows in
Postgres, `data/bm25.pkl`, and an `index_manifest.json` that every F4 eval run records so all
numbers are reproducible. F2 does **not** retrieve, rerank, or generate — those are F3+.

---

## 2. User stories

- **US-1 (Indexing operator):** As the operator, I want one command to chunk, embed, upsert, and
  build BM25 for the whole corpus so the index is reproducible from F1 output.
- **US-2 (Retrieval researcher):** As the person tuning retrieval quality, I want two comparable
  chunking strategies behind a flag so I can A/B `fixed` vs `structure` under the F4 harness.
- **US-3 (Downstream F3 developer):** As the retriever author, I want every chunk to carry
  `chunk_id`, page/anchor, and `section_heading` so a `Citation` resolves to an exact location
  with zero re-parsing.
- **US-4 (Downstream F3 developer):** As the retriever author, I want chunk metadata in Postgres
  (not only Pinecone) so citation/eval lookups are a cheap DB read, not a vector round-trip.
- **US-5 (Downstream F5 developer):** As the hybrid-retrieval author, I want a persisted BM25
  index (`data/bm25.pkl`) keyed by the same `chunk_id`s so sparse and dense results fuse cleanly.
- **US-6 (Cost owner):** As the person paying the OpenAI bill, I want per-run token and dollar
  totals printed so embedding cost is visible and logged on every call.
- **US-7 (Indexing operator):** As the operator, I want embedding to fan out concurrently but
  stay bounded and back off on 429 so a full-corpus run is fast without tripping rate limits.
- **US-8 (Indexing operator):** As the operator, I want a strategy change to force a wipe so I
  never end up with a mixed-strategy index that silently corrupts comparisons.
- **US-9 (Auditor):** As anyone reproducing a benchmark, I want an `index_manifest.json`
  (strategy, embed model, counts, timestamp) so every eval label maps to a known index state.
- **US-10 (Ops):** As an operator, I want CPU-bound work (BM25 build/pickle) kept off the event
  loop and I/O (embeddings, upserts, DB commits) fully async so a large run stays responsive.

---

## 3. EARS acceptance criteria

### 3.1 Chunking strategies
- **AC-1 (Ubiquitous):** The system shall support two strategies selected by the
  `INDEXING_STRATEGY` flag / `--strategy` CLI arg: `fixed` and `structure`.
- **AC-2 (Event-driven — fixed):** When strategy is `fixed`, the system shall split with
  `RecursiveCharacterTextSplitter.from_tiktoken_encoder` at **500 tokens, 50 overlap**.
- **AC-3 (Event-driven — structure):** When strategy is `structure`, the system shall split
  heading-aware: HTML/DOCX use the `section_heading` carried on F1 blocks; PDFs use regex clause
  detection (`"12."`, `"12.3"`, ALL-CAPS headings, `"Regulation No."`) to open sections.
- **AC-4 (State-driven — oversize section):** While a `structure` section exceeds
  `STRUCTURE_MAX_SECTION_TOKENS` (800), the system shall re-split that section with the `fixed`
  splitter, preserving the parent `section_heading` on each resulting chunk.
- **AC-5 (Ubiquitous):** The system shall propagate `doc_id`, `page_start`/`page_end`, `anchor`,
  and `section_heading` from the source block onto every chunk it produces.
- **AC-6 (Ubiquitous):** The system shall assign `chunk_id = {doc_id}:{seq}` with `seq` a
  zero-based counter that is contiguous and stable within a document for a given strategy, and
  compute `token_count` per chunk via tiktoken `cl100k_base`.
- **AC-7 (Unwanted — oversize chunk):** If a single chunk still exceeds
  `EMBED_MAX_CHUNK_TOKENS` (8000) after splitting, then the system shall truncate it at that
  limit and emit a warning (guards the embedding + Pinecone metadata limits).
- **AC-8 (Unwanted — empty section):** If a `structure` section is empty or below
  `CLEAN_MIN_BLOCK_CHARS`, then the system shall merge it forward into the next section rather
  than emit an empty chunk.

### 3.2 Embedding (async)
- **AC-9 (Ubiquitous):** The system shall embed chunk text with
  `OpenAIEmbeddings(model="text-embedding-3-small")` via **`aembed_documents`** only (the sync
  `embed_documents` is banned in `backend/app/`).
- **AC-10 (Event-driven):** When embedding a document's chunks, the system shall batch **100**
  texts per request and dispatch batches concurrently with `asyncio.gather` bounded by
  `asyncio.Semaphore(EMBED_CONCURRENCY)` (default 4).
- **AC-11 (Event-driven — 429):** When the embeddings API returns 429 / a rate-limit error, the
  system shall retry with exponential backoff (`tenacity` async) up to `EMBED_MAX_RETRIES`.
- **AC-12 (Ubiquitous):** The system shall log token usage and estimated USD cost for every
  embedding call via the central `estimate_cost()` helper, and print a run-level total.

### 3.3 Pinecone upsert
- **AC-13 (Ubiquitous):** The system shall upsert vectors to a Pinecone **serverless** index
  (dimension 1536, metric cosine) using `langchain-pinecone` `PineconeVectorStore` with the
  **async** client surface (`aadd_documents` / async upsert).
- **AC-14 (Ubiquitous):** The system shall set **namespace = `source_org`** (`pu` / `hec`),
  **vector id = `chunk_id`**, and metadata `{doc_id, title, section_heading, page_start,
  page_end, anchor, token_count, text}`.
- **AC-15 (Unwanted — metadata limit):** If a chunk's assembled metadata would exceed Pinecone's
  40 KB per-vector limit (dominated by `text`), then the system shall truncate the stored `text`
  metadata to fit and warn, without dropping the vector.
- **AC-16 (Event-driven):** When upserting, the system shall batch vectors and await batches with
  the same bounded-`gather` pattern as embedding; transient upsert rate limits shall back off.

### 3.4 BM25 corpus
- **AC-17 (Ubiquitous):** The system shall build a `rank-bm25` index over all chunk texts with a
  tokenizer that **keeps Urdu words intact** (no ASCII-only stripping) and lowercases Latin text.
- **AC-18 (Event-driven):** When indexing completes, the system shall pickle the BM25 object plus
  the aligned `chunk_id` list to `data/bm25.pkl` — the ordinal position in the corpus maps back
  to a `chunk_id` so F5 can resolve sparse hits.
- **AC-19 (Ubiquitous):** The system shall build and pickle BM25 off the event loop via
  `anyio.to_thread.run_sync` (CPU/disk-bound work), matching the F1/lifespan threading rule.

### 3.5 Postgres chunks
- **AC-20 (Event-driven):** When a document is indexed, the system shall write one `chunks` row
  per chunk (`chunk_id, doc_id, seq, text, section_heading, page_start, page_end, anchor,
  token_count`) via async-session commits in the same run.
- **AC-21 (Ubiquitous):** The system shall keep Pinecone vector count == Postgres `chunks` count
  per namespace after a successful run (the reconciliation invariant).
- **AC-22 (Event-driven):** When re-indexing a document, the system shall delete that document's
  prior `chunks` rows and Pinecone vectors before writing new ones (no orphans, no duplicates).
- **AC-23 (Event-driven):** When a document reaches a fully indexed state, the system shall set
  its `documents.status = indexed`.

### 3.6 Manifest, wipe & strategy safety
- **AC-24 (Event-driven):** When a run finishes, the system shall write
  `data/index_manifest.json` recording `strategy`, `embed_model`, per-namespace vector/chunk
  counts, total tokens, estimated cost, and an ISO timestamp.
- **AC-25 (Unwanted — strategy drift):** If the requested `--strategy` differs from the strategy
  recorded in the existing manifest and `--wipe` was **not** passed, then the system shall abort
  before any upsert with a strategy-mismatch error (no mixed populations).
- **AC-26 (Event-driven — wipe):** When `--wipe` is passed, the system shall delete the target
  namespace's Pinecone vectors and the corresponding `chunks` rows before re-indexing.

### 3.7 CLI & concurrency
- **AC-27 (Ubiquitous):** The system shall expose
  `python -m app.indexing.run --strategy fixed|structure --namespace pu|hec|all [--wipe]` with
  entrypoint `asyncio.run(main())`.
- **AC-28 (Ubiquitous):** The system shall await all embedding, upsert, and DB writes on the loop,
  and run only the BM25 build/pickle off the loop via `anyio.to_thread.run_sync`; tiktoken
  counting and the text splitters (pure CPU, cheap) may run inline.
- **AC-29 (Unwanted):** If F1 output is missing for a document (no `data/extracted/{doc_id}.jsonl`
  or `status != extracted`), then the system shall skip that document with a warning and continue,
  never fabricating chunks.

---

## 4. Acceptance criteria (feature-level definition of done)

1. **One command** indexes the full corpus from F1 output; on completion **Pinecone vector
   counts == Postgres `chunks` counts** per namespace and the manifest is written.
2. The `structure` strategy attaches a `section_heading` to **≥ 60%** of PU Calendar chunks
   (asserted programmatically on the fixture set).
3. **Embedding cost is printed per run** (tokens + estimated USD) and logged via `estimate_cost()`.
4. A **strategy change forces a wipe** (AC-25): re-running with a different `--strategy` and no
   `--wipe` aborts loudly; with `--wipe` it re-indexes cleanly.
5. `data/bm25.pkl` loads and its `chunk_id` list aligns 1:1 with the indexed chunks; committed
   fixtures let the whole flow run against a mocked embeddings/Pinecone client.

---

## 5. Out of scope (do not implement here)

- **Retrieval** (dense/sparse query, fusion, rerank, generation) → F3 / F5 / F6.
- **Embedding-based semantic chunking** — stretch note only; F2 ships `fixed` + `structure`.
- Re-parsing source files — F2 consumes F1's JSONL contract and never re-runs loaders.
- Query-time BM25 scoring — F2 only *builds and persists* the corpus; F5 loads and queries it.
- Any second embedding model or dimension — `text-embedding-3-small` (1536) is fixed.
