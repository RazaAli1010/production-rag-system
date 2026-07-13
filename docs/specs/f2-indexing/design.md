# F2 — Chunking & Indexing · design.md

**Module:** `backend/app/indexing/` · **Depends on:** F1 · **Blocks:** F3

---

## 1. Module layout

```
backend/app/indexing/
├── __init__.py
├── run.py            # CLI entrypoint: asyncio.run(main()); arg parsing; orchestration
├── source.py         # read data/extracted/{doc_id}.jsonl -> list[Document] (F1 seam)
├── chunkers/
│   ├── __init__.py
│   ├── base.py       # Chunker protocol: split(docs, doc_id) -> list[Chunk]
│   ├── fixed.py      # RecursiveCharacterTextSplitter.from_tiktoken_encoder (500/50)
│   └── structure.py  # heading-aware: HTML/DOCX headings, PDF clause regex, >800tok re-split
├── embedder.py       # OpenAIEmbeddings.aembed_documents, batch 100, bounded gather, 429 backoff
├── vectorstore.py    # PineconeVectorStore async upsert, namespace/id/metadata, 40KB guard, wipe
├── bm25.py           # Urdu-safe tokenizer, build + pickle to data/bm25.pkl (threaded)
├── persistence.py    # async-session write/delete of `chunks` rows; status=indexed transition
├── manifest.py       # write/read data/index_manifest.json; strategy-drift guard
├── cost.py           # estimate_cost() for embedding tokens (central helper; reused by F3+)
└── schemas.py        # F2-local pydantic (IndexResult, RunReport, Manifest); re-exports Chunk
```

Shared/canonical models (`Chunk`, `DocumentMeta`, `Citation`, …) are imported from the
project-wide contracts / `app.db.models`, **not** redefined here. `cost.estimate_cost()` is
introduced by F2 (first OpenAI-calling feature) as the single central cost helper mandated by
CLAUDE.md, and is reused by every later OpenAI-calling feature (F3, F7, F8, F9, F17).

---

## 2. Data-flow diagram

```
                data/extracted/{doc_id}.jsonl   (F1 output — the seam)
                            │
                    source.load_blocks(doc_id)  ──►  list[langchain.Document]
                            │
              chunkers.select(strategy).split(docs, doc_id)
                 fixed.py            structure.py
             (500tok/50 overlap)  (headings / clause regex; >800tok -> fixed re-split)
                            │  list[Chunk]  (chunk_id={doc_id}:{seq}, token_count, page/anchor/heading)
                            ▼
   ┌────────────────────────┼─────────────────────────┐
   ▼                        ▼                          ▼
 embedder.embed        persistence.write_chunks    (accumulate texts + ids
 (aembed_documents,    (async session: delete old   for the BM25 corpus)
  batch 100, Sem(4),    rows, insert new rows)
  429 backoff, cost)         │
   │  vectors                ▼
   ▼                    documents.status = indexed
 vectorstore.upsert
 (async PineconeVectorStore,
  namespace=source_org, id=chunk_id,
  metadata + 40KB text guard, Sem(4))
   │
   └──────────────► (after all docs) ──────────────►
                            │
                    bm25.build_and_pickle(texts, chunk_ids)  ──►  data/bm25.pkl
                    (Urdu-safe tokenizer; anyio.to_thread.run_sync)
                            │
                    manifest.write(strategy, model, counts, tokens, cost, ts)
                            │
                    reconcile: Pinecone count == chunks count  (AC-21) ──► run report
```

CPU/disk-bound nodes (`bm25.build_and_pickle`) run via `anyio.to_thread.run_sync`. Everything
else — `aembed_documents`, async Pinecone upsert, async SQLAlchemy commits — is awaited on the
loop. Fan-out (per-batch embed, per-batch upsert) uses `asyncio.gather` bounded by
`asyncio.Semaphore(EMBED_CONCURRENCY)` / `Semaphore(PINECONE_UPSERT_CONCURRENCY)`. The text
splitters and tiktoken counting are cheap pure-CPU and run inline (per the CLAUDE.md
inline-vs-thread line).

---

## 3. Chunking strategy table (JD showcase — required)

| Source (F1 block) | `fixed` behavior | `structure` behavior | `section_heading` | Page / anchor |
|---|---|---|---|---|
| PDF digital | `Recursive…from_tiktoken_encoder`, 500/50 | clause regex (`12.`, `12.3`, ALL-CAPS, `Regulation No.`) opens sections; >800 tok → fixed re-split | best-effort from clause/heading | `page_start`/`page_end` propagated |
| PDF scanned (post-OCR) | same as digital | same as digital | best-effort | pages propagated (post-OCR) |
| HTML | 500/50 over block text | split on F1 `section_heading`; >800 tok → fixed re-split | F1 heading text | `anchor` (`#id`) propagated |
| DOCX | 500/50 | split on F1 heading elements | F1 heading text | `None` |
| PPTX | 500/50 | one section per slide (F1 `anchor=slide-{n}`) | slide title | `anchor` = slide no. |
| XLSX | 500/50 over row-wise blocks | row-wise blocks grouped by sheet | `None` | `anchor` = sheet name |

`structure` PDF clause regex (config `STRUCTURE_CLAUSE_PATTERNS`): numbered clauses
(`^\d+(\.\d+)*[.)]\s`), ALL-CAPS heading lines, and the literal `Regulation No.` marker open a
new section; text before the first match joins a leading "preamble" section. Sections over
`STRUCTURE_MAX_SECTION_TOKENS` (800) are re-split by the `fixed` splitter with the parent
`section_heading` copied onto each resulting chunk (AC-4). Empty/short sections merge forward
(AC-8).

---

## 4. Key function signatures

```python
# source.py
async def load_blocks(doc_id: str, settings: Settings) -> list[Document]: ...   # aiofiles read
async def indexed_targets(session, namespace: str, settings) -> list[DocumentMeta]: ...

# chunkers/base.py
class Chunker(Protocol):
    def split(self, docs: list[Document], doc_id: str) -> list[Chunk]: ...   # pure CPU, inline
def select_chunker(strategy: str, settings: Settings) -> Chunker: ...

# chunkers/fixed.py  /  structure.py
class FixedChunker:     def split(self, docs, doc_id) -> list[Chunk]: ...
class StructureChunker: def split(self, docs, doc_id) -> list[Chunk]: ...
def _resplit_oversize(section: Chunk, settings: Settings) -> list[Chunk]: ...   # >800 tok

# embedder.py
async def embed_chunks(
    chunks: list[Chunk], embeddings: OpenAIEmbeddings,
    gate: asyncio.Semaphore, settings: Settings,
) -> list[list[float]]: ...                         # aembed_documents, batch 100, 429 backoff
def _batch(items: list, n: int) -> Iterator[list]: ...   # pure CPU, inline

# vectorstore.py
async def upsert(
    store: PineconeVectorStore, chunks: list[Chunk], vectors: list[list[float]],
    namespace: str, gate: asyncio.Semaphore, settings: Settings,
) -> int: ...                                       # returns vectors upserted
def _build_metadata(chunk: Chunk, title: str, settings: Settings) -> dict: ...  # 40KB text guard
async def wipe_namespace(store, session, namespace: str, settings) -> None: ...

# bm25.py  (CPU/disk -> to_thread)
def build_and_pickle(texts: list[str], chunk_ids: list[str], settings: Settings) -> Path: ...
def urdu_safe_tokenize(text: str) -> list[str]: ...   # pure CPU

# persistence.py
async def replace_chunks(session, doc_id: str, chunks: list[Chunk]) -> None: ...  # delete+insert
async def mark_indexed(session, doc_id: str) -> None: ...

# cost.py
def estimate_cost(model: str, tokens_in: int, tokens_out: int = 0) -> float: ...  # central helper

# manifest.py
def write_manifest(m: Manifest, settings: Settings) -> Path: ...
def read_manifest(settings: Settings) -> Manifest | None: ...
def guard_strategy(requested: str, wipe: bool, settings: Settings) -> None: ...   # AC-25 abort

# run.py
async def index_one(session, store, embeddings, row, chunker, gates, settings) -> IndexResult: ...
async def main() -> None: ...        # arg parse, per-doc pipeline, BM25 build, manifest, report
```

`IndexResult`, `RunReport`, `Manifest` live in `indexing/schemas.py`. Every `Chunk` conforms to
the shared contract: `chunk_id, doc_id, seq, text, section_heading, page_start, page_end, anchor,
token_count`.

---

## 5. LCEL composition & the F3 retriever seam

F2, like F1, is an offline pipeline rather than a runtime LCEL graph, so it uses LangChain
**splitters + embeddings + vector store** rather than an `ainvoke`/`astream` chain. The LCEL
surface it touches and the seam it hands to F3:

- **Splitters:** `RecursiveCharacterTextSplitter.from_tiktoken_encoder` is used directly; it is
  pure-CPU and cheap, so it runs inline (no thread offload needed) per the CLAUDE.md line.
- **Embeddings:** only the async surface `aembed_documents` is called (sync `embed_documents` is
  CI-banned in `backend/app/`).
- **Vector store:** `PineconeVectorStore` is populated here so that in **F3 the same store is
  wrapped as a `BaseRetriever`** via `.as_retriever(search_kwargs={"namespace": …})`. That
  retriever is the LCEL seam F3 composes into its chain — F2's obligation is that the store is
  populated with vectors whose `id == chunk_id` and whose metadata carries every `Citation`
  field, and that the parallel **`chunks` table + `bm25.pkl`** exist so F5's hybrid retriever
  (dense store + sparse BM25) fuses on a shared `chunk_id` key. F2 defines no chain; the async
  rule is honored via awaited I/O + the one threaded BM25 build.

---

## 6. Error handling

| Failure | Detection | Handling | Status / report |
|---|---|---|---|
| Missing/`!extracted` F1 output | `source.load_blocks` / status check | skip doc, warn, continue | doc left non-`indexed`, listed in report |
| Oversize chunk (>8000 tok) | tiktoken count in chunker | truncate at limit, warn | counted in report |
| Empty/short `structure` section | length check | merge forward | — |
| Embeddings 429 / rate limit | `tenacity` retry predicate | backoff ×`EMBED_MAX_RETRIES` | retried, then fails doc if exhausted |
| Metadata > 40 KB | `_build_metadata` size check | truncate stored `text`, warn | counted in report |
| Pinecone upsert rate limit | async client error | batch + backoff | retried |
| Strategy drift, no `--wipe` | `guard_strategy` vs manifest | **abort run** before upsert | loud strategy-mismatch error |
| Count mismatch (Pinecone ≠ chunks) | post-run reconcile (AC-21) | fail the run loudly | reconciliation error in report |
| Partial doc failure mid-run | exception per `index_one` | isolate doc, continue batch | `failed`/non-`indexed`, listed |

Per-document failures are isolated (one bad doc never fails the batch) except the two loud-abort
cases (strategy drift, post-run count mismatch), which are correctness violations.

---

## 7. New Settings keys (central `app.core.settings.Settings`)

F2 is the first feature to call OpenAI and Pinecone, so it introduces those secrets plus the
indexing tunables. All values read via the one Pydantic `Settings` class (env-overridable).

```python
# --- OpenAI / embeddings (F2; reused by F3+) ---
OPENAI_API_KEY: SecretStr
EMBED_MODEL: str = "text-embedding-3-small"   # 1536-dim, fixed
EMBED_DIM: int = 1536
EMBED_BATCH_SIZE: int = 100
EMBED_CONCURRENCY: int = 4                     # bounded gather for embed batches
EMBED_MAX_RETRIES: int = 5                     # 429 backoff attempts
EMBED_MAX_CHUNK_TOKENS: int = 8000             # truncate-and-warn guard (AC-7)

# --- Pinecone (F2) ---
PINECONE_API_KEY: SecretStr
PINECONE_INDEX: str
PINECONE_UPSERT_CONCURRENCY: int = 4
PINECONE_METADATA_MAX_BYTES: int = 40_000      # 40KB per-vector limit guard (AC-15)

# --- Chunking (F2) ---
INDEXING_STRATEGY: Literal["fixed", "structure"] = "fixed"   # --strategy overrides
FIXED_CHUNK_TOKENS: int = 500
FIXED_CHUNK_OVERLAP: int = 50
STRUCTURE_MAX_SECTION_TOKENS: int = 800        # over this -> fixed re-split (AC-4)
STRUCTURE_CLAUSE_PATTERNS: list[str] = [        # PDF clause detection (AC-3)
    r"^\d+(\.\d+)*[.)]\s", r"^[A-Z][A-Z \-]{6,}$", r"Regulation No\.",
]

# --- Artifacts (F2) ---
BM25_PATH: Path = Path("app/data/bm25.pkl")
INDEX_MANIFEST_PATH: Path = Path("app/data/index_manifest.json")
```

`EMBED_MAX_CHUNK_TOKENS`, `CLEAN_MIN_BLOCK_CHARS` (reused from F1 for the merge-forward rule),
and the clause patterns are the only chunk-shaping knobs; no module-level constants outside
`Settings`.

---

## 8. Alembic migration

The `chunks` table is **owned by F12** and already carries every column F2 writes
(`chunk_id, doc_id, seq, text, section_heading, page_start, page_end, anchor, token_count`,
with `ix_chunks_doc_id_seq`). The `documents.status` enum **already includes `indexed`**
(`app/db/enums.py`). Therefore:

- **F2 introduces no new table or column** — it is a pure consumer/writer of the F12 schema, so
  under the project rule "every schema change is an Alembic migration" there is simply no schema
  change and hence no migration to author. This is deliberate and called out here so a reviewer
  doesn't expect one.
- If profiling later shows citation/eval lookups need it, an **optional** additive migration may
  add `ix_chunks_doc_id` alone; it is not part of F2's DoD and is noted, not implemented.

The `index_manifest.json` and `bm25.pkl` are filesystem artifacts by design (not Postgres) —
BM25 is an in-process `rank-bm25` object per the fixed stack, and the manifest is a
reproducibility record consumed by the F4 harness, not queried relationally.

---

## 9. Honoring the shared-context contracts

- **`Chunk`**: F2 produces exactly the canonical `Chunk` (`chunk_id={doc_id}:{seq}`,
  `token_count` via tiktoken `cl100k_base`) and persists it to the `chunks` table — no re-parse
  of source files, only F1's JSONL.
- **`Citation` readiness**: because F2 copies `title` (from the `documents` row), `section_heading`,
  `page_start/page_end`, `anchor`, and `url` into both Postgres and Pinecone metadata, F3 can
  build a full `Citation` from a retrieved chunk with a single cheap DB read (AC-4 / US-4).
- **`RetrievedChunk`**: F2 stores **no** score columns — dense/sparse/fused/rerank scores are
  transient runtime fields recomputed per query by F3/F5/F6, exactly as the contract notes.
- **ID convention**: chunk id `{doc_id}:{chunk_seq}`; namespace = `source_org` (`pu`/`hec`);
  Pinecone vector id == `chunk_id` == BM25 corpus key, so all three stores join cleanly.
- **Async rule**: awaited `aembed_documents`, async Pinecone upsert, async SQLAlchemy commits;
  threaded BM25 build/pickle (`anyio.to_thread.run_sync`); inline pure-CPU (text splitters,
  tiktoken counting, batching, metadata assembly). Each is annotated in §2/§4 with its side of
  the line.
- **Cost rule**: `cost.estimate_cost()` is the single central helper CLAUDE.md mandates; every
  `aembed_documents` call logs `tokens_in` + estimated USD, and the run prints the total (AC-12).

---

## 10. Test strategy (see tasks.md for the ordered list)

- Fixtures reuse F1's committed `data/extracted/*.jsonl` outputs under
  `backend/tests/fixtures/indexing/` (a small PU-Calendar-like PDF export for the 60% heading
  assertion, plus one of each other type).
- Unit tests: strategy selection; `fixed` token/overlap boundaries; `structure` clause regex +
  >800-token re-split + heading propagation; `chunk_id`/`seq` contiguity; oversize truncation;
  40 KB metadata guard; Urdu-safe tokenizer; `estimate_cost()` math; strategy-drift abort.
- Integration test (mocked `OpenAIEmbeddings.aembed_documents` + fake Pinecone store): full
  `index_one` over each fixture writes `chunks` rows, upserts fake vectors, and yields
  `status=indexed`; **Pinecone-mock count == `chunks` count** asserted; BM25 pickle round-trips
  and its `chunk_id` list aligns 1:1.
- CLI test: `--strategy`, `--namespace pu|hec|all`, `--wipe` paths; strategy change without
  `--wipe` aborts; with `--wipe` deletes prior vectors/rows then re-indexes.
