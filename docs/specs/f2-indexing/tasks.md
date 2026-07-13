# F2 — Chunking & Indexing · tasks.md

**Module:** `backend/app/indexing/` · **Depends on:** F1 · **Blocks:** F3
Each task is scoped to ≈ ≤ 1 hour and carries its own test criterion. F2 is Phase A, so there is
**no eval-gate task** (the F4 `--compare`/delta gate applies only to Phase B/C enhancements and
begins at F5). F2's job is to *build the index those evals will measure*.

Ordering follows the data flow: settings/schemas → source seam → chunkers → cost → embedder →
vectorstore → persistence → bm25 → manifest/wipe → CLI → fixtures → acceptance.

---

### T1 — Settings + F2 schemas
Add the F2 keys from `design.md §7` to the central `Settings` class (OpenAI/embedding, Pinecone,
chunking, artifact paths); create `indexing/schemas.py` (`IndexResult`, `RunReport`, `Manifest`)
and re-export the shared `Chunk`.
**Test:** `Settings()` loads all new keys with defaults + env overrides (secrets as `SecretStr`);
schema models round-trip via pydantic; `pytest tests/indexing/test_settings_schemas.py` green.

### T2 — Source seam: read F1 JSONL
Implement `source.load_blocks(doc_id)` (aiofiles read of `data/extracted/{doc_id}.jsonl` →
`list[Document]`) and `source.indexed_targets()` (select `documents` where `status=extracted`,
filtered by namespace). Missing file / non-`extracted` → skip + warn (AC-29).
**Test:** valid JSONL → Documents with canonical metadata; missing file → empty + warning, no raise.

### T3 — Chunker base + selection
Implement `chunkers/base.py` (`Chunker` protocol, `chunk_id={doc_id}:{seq}`, tiktoken
`cl100k_base` `token_count`) and `select_chunker(strategy)`.
**Test:** `select_chunker("fixed")`/`("structure")` return the right class; unknown → raises;
`seq` is contiguous & zero-based across a multi-block doc.

### T4 — Fixed chunker
Implement `chunkers/fixed.py` via `RecursiveCharacterTextSplitter.from_tiktoken_encoder`
(500 tokens / 50 overlap), propagating `doc_id`/page/anchor/`section_heading` onto each chunk.
**Test:** a long block yields chunks ≤ 500 tokens with ~50-token overlap; metadata propagated;
oversize single chunk truncated at `EMBED_MAX_CHUNK_TOKENS` with a warning (AC-7).

### T5 — Structure chunker: headings
Implement `chunkers/structure.py` heading path for HTML/DOCX/PPTX/XLSX using F1
`section_heading`/`anchor`; empty/short sections merge forward (AC-8).
**Test:** HTML/DOCX fixtures split on headings with `section_heading` set; an empty section
merges forward rather than emitting an empty chunk.

### T6 — Structure chunker: PDF clause regex + re-split
Add PDF clause detection (`STRUCTURE_CLAUSE_PATTERNS`) opening sections; sections
> `STRUCTURE_MAX_SECTION_TOKENS` (800) re-split with the fixed splitter, parent `section_heading`
copied onto each child (AC-4).
**Test:** a numbered-clause PDF fixture opens a section per clause; an 1100-token section yields
multiple ≤500-token chunks all carrying the parent heading.

### T7 — Central cost helper
Implement `cost.estimate_cost(model, tokens_in, tokens_out=0)` with `text-embedding-3-small` /
`gpt-4o-mini` / `gpt-4o` rates (the single central helper reused by F3+).
**Test:** known token counts → expected USD to 6 dp for each model; unknown model → controlled error.

### T8 — Async embedder
Implement `embedder.embed_chunks` via `OpenAIEmbeddings.aembed_documents`, batch 100
(`_batch`), concurrent batches under `asyncio.Semaphore(EMBED_CONCURRENCY)`, `tenacity` async
429 backoff; log tokens + `estimate_cost()` per call and a run total (AC-9–AC-12).
**Test (mocked embeddings):** 250 chunks → 3 batches, all awaited; injected 429 retried then
succeeds; cost logged per call; **no sync `embed_documents`** referenced (grep assertion).

### T9 — Pinecone vector store + metadata guard
Implement `vectorstore.upsert` via async `PineconeVectorStore` (namespace=`source_org`,
id=`chunk_id`, metadata per AC-14), bounded gather, and `_build_metadata` truncating stored
`text` to `PINECONE_METADATA_MAX_BYTES` (AC-15).
**Test (fake store):** vectors upserted with id==chunk_id and namespace==source_org; a giant
chunk's metadata truncated under 40 KB with a warning; batch backoff on injected rate limit.

### T10 — Postgres chunk persistence + status
Implement `persistence.replace_chunks` (delete prior `doc_id` rows, insert new) and
`mark_indexed` (`documents.status=indexed`), async-session commits (AC-20/AC-22/AC-23).
**Test:** re-indexing a doc leaves exactly the new rows (no orphans/dupes); status flips to
`indexed`; rows match the shared `Chunk` columns.

### T11 — BM25 build + pickle (threaded)
Implement `bm25.urdu_safe_tokenize` and `build_and_pickle(texts, chunk_ids)` (rank-bm25 +
aligned `chunk_id` list) pickled to `data/bm25.pkl` via `anyio.to_thread.run_sync` (AC-17–AC-19).
**Test:** Urdu string tokenizes without stripping; pickle round-trips; loaded `chunk_id` list
aligns 1:1 with input order; build runs off the loop (threaded).

### T12 — Manifest + strategy-drift guard + wipe
Implement `manifest.write_manifest`/`read_manifest`, `guard_strategy` (abort if requested
strategy ≠ manifest and no `--wipe`, AC-25), and `vectorstore.wipe_namespace` (delete namespace
vectors + `chunks` rows, AC-26).
**Test:** manifest written with strategy/model/counts/cost/ts; strategy change without `--wipe`
raises the mismatch error; with `--wipe` prior vectors and rows are deleted.

### T13 — CLI orchestration + reconciliation
Implement `run.main()` + `index_one`: arg parsing (`--strategy fixed|structure`,
`--namespace pu|hec|all`, `--wipe`), `asyncio.run(main())`, per-doc pipeline (chunk → embed →
upsert + persist), then BM25 build, manifest write, and the **Pinecone count == `chunks` count**
reconciliation (AC-21/AC-27/AC-28); print token + USD total.
**Test (mocked embed + fake store):** `--namespace all` indexes fixtures; count reconciliation
passes; a per-doc failure is isolated; run prints embedding cost.

### T14 — Structlog observability
Emit `structlog` per-doc events (chunk count, tokens, per-stage ms) and the run-level
tokens/cost summary; confirm every embedding call logs cost via `estimate_cost()`.
**Test:** log capture shows one structured event per doc plus a run total; every embed call
carries a cost field.

### T15 — Commit indexing fixtures
Add small committed fixtures under `backend/tests/fixtures/indexing/`: F1-style
`extracted/*.jsonl` for one of each type **plus a PU-Calendar-like clause-numbered PDF export**
(for the ≥60% heading assertion). Keep total size repo-friendly.
**Test:** fixtures present and loadable via `source.load_blocks`; sizes small.

### T16 — Acceptance / definition of done
Wire an end-to-end integration test (mocked `aembed_documents` + fake Pinecone) proving the
feature-level ACs:
1. one command indexes the full fixture corpus; **Pinecone-mock count == `chunks` count** per
   namespace; manifest written;
2. `structure` attaches `section_heading` to **≥ 60%** of PU-Calendar-fixture chunks (asserted);
3. embedding cost printed per run (tokens + USD) and logged via `estimate_cost()`;
4. strategy change forces a wipe — re-run with a new `--strategy` and no `--wipe` aborts; with
   `--wipe` re-indexes cleanly;
5. `data/bm25.pkl` loads and its `chunk_id` list aligns 1:1 with the indexed chunks.
**Definition of done:** `pytest tests/indexing/` green including the end-to-end test, all five
feature-level acceptance criteria asserted, and (per §8) confirmation that F2 requires **no** new
Alembic migration because the `chunks` table + `indexed` status are already owned by F12.

---

**No eval gate:** F2 is Phase A foundation. The mandatory F4 `--compare`/delta-report gate begins
at Phase B (F5 Hybrid retrieval) and is not part of this feature's DoD. F2's deliverable is the
reproducible index (vectors + `chunks` + `bm25.pkl` + `index_manifest.json`) that every later
eval label maps to.
