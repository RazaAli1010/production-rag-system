# F1 — Multi-Format Ingestion Pipeline · design.md

**Module:** `backend/app/ingestion/` · **Depends on:** F12 · **Blocks:** F2

---

## 1. Module layout

```
backend/app/ingestion/
├── __init__.py
├── run.py            # CLI entrypoint: asyncio.run(main()); arg parsing; orchestration
├── registry.py       # sources.csv parse + validate + upsert into `documents`
├── downloader.py     # async httpx fetch, sha256 dedupe, rate limit, retries, sniff
├── routing.py        # file_type -> loader dispatch table (the JD showcase table)
├── loaders/
│   ├── __init__.py
│   ├── pdf.py        # PyMuPDFLoader fast path + reading-order sort
│   ├── ocr.py        # scanned detection + ocrmypdf (eng+urd), per-page selective OCR
│   ├── html.py       # UnstructuredHTMLLoader (+ BSHTMLLoader fallback), anchors
│   ├── office.py     # docx / pptx / xlsx via unstructured loaders
│   └── legacy.py     # .doc/.ppt -> libreoffice --headless conversion
├── cleaning.py       # header/footer strip, de-hyphenate, whitespace, NFC, min-len drop
├── serialize.py      # write data/extracted/{doc_id}.jsonl via aiofiles
├── status.py         # documents.status transitions + per-doc note + run report
└── schemas.py        # F1-local pydantic (IngestResult, RunReport); re-exports shared models
```

Shared/canonical models (`DocumentMeta`, `Chunk`, `Citation`, …) are imported from the
project-wide contracts module (`app.core.contracts` / `app.db.models`), **not** redefined here.

---

## 2. Data-flow diagram

```
                       sources.csv
                            │
                     registry.load_sources()  ──►  upsert documents (status=registered)
                            │
              ┌─────────────┴──────────────┐   (asyncio.gather, bounded by Semaphore)
              ▼                            ▼
     downloader.fetch(doc)         downloader.fetch(doc) ...
   (Semaphore(1)+sleep, retries)
              │  raw bytes -> data/raw/{doc_id}.{ext}, sha256
              ▼
     status: downloaded
              │
     routing.select_loader(file_type)
              │
   ┌──────────┼───────────┬───────────┬───────────┐
   ▼          ▼           ▼           ▼           ▼
 pdf.py    ocr.py      html.py     office.py    legacy.py
(fast)  (scan detect →           (docx/pptx/   (.doc/.ppt →
        ocrmypdf → reload)        xlsx)         libreoffice → re-route)
   └──────────┴───────────┴───────────┴───────────┘
              │  List[langchain.Document] (raw)
              ▼
        cleaning.clean(docs)   (header/footer, de-hyphen, NFC, drop <20 chars)
              │
              ▼
     serialize.write_jsonl(doc_id, docs)  ──►  data/extracted/{doc_id}.jsonl
              │
     status: extracted  (+ page_count, is_scanned, sha256, downloaded_at)
              │
              ▼
        status.build_report()  ──►  stdout + docs/ingestion_report_{ts}.md
```

CPU/subprocess-heavy nodes (`ocr.py`, `office.py`, `legacy.py`, `unstructured` parsing) run via
`anyio.to_thread.run_sync`. Everything else (`httpx`, `aiofiles`, async SQLAlchemy) is awaited on
the loop. Fan-out uses `asyncio.gather` bounded by `asyncio.Semaphore(INGEST_CONCURRENCY)`; the
download rate limit is a separate `asyncio.Semaphore(1)` + sleep so concurrency never breaches
1 req/sec.

---

## 3. Loader routing table (JD showcase — required)

| file_type | Primary loader | Fallback / special path | Page fields | `anchor` | `section_heading` |
|---|---|---|---|---|---|
| `pdf` (digital) | `PyMuPDFLoader` | reading-order sort for 2-column pages | `page_start`/`page_end` set | `None` | best-effort from font/size heuristics |
| `pdf` (scanned) | `ocrmypdf` (Tesseract `eng+urd`) → `PyMuPDFLoader` | selective per-page OCR for mixed docs | set post-OCR | `None` | best-effort |
| `html` | `UnstructuredHTMLLoader` | `BSHTMLLoader` | `None` | heading anchor (`#id`) | heading text |
| `docx` | `UnstructuredWordDocumentLoader` | — | `None` | `None` | heading element text |
| `pptx` | `UnstructuredPowerPointLoader` | — | `None` | slide number (`slide-{n}`) | slide title |
| `xlsx` | `UnstructuredExcelLoader` | — | `None` | sheet name | `None` |
| `.doc`/`.ppt` (legacy) | `libreoffice --headless` → `.docx`/`.pptx` | mark unsupported if convert fails | inherits target type | inherits | inherits |

Scanned detection rule (AC-13): a page is "scanned" when extractable text `< OCR_MIN_PAGE_TEXT_CHARS`
(50) **and** it contains ≥ 1 image XObject; if scanned pages `> OCR_SCANNED_PAGE_THRESHOLD` (0.30)
of total, the document is `is_scanned = true` and OCR runs on the scanned pages only.

---

## 4. Key function signatures

```python
# registry.py
async def load_sources(csv_path: Path) -> list[SourceRow]: ...
async def upsert_documents(session: AsyncSession, rows: list[SourceRow]) -> None: ...

# downloader.py
async def fetch(
    client: httpx.AsyncClient, row: SourceRow,
    rate_gate: asyncio.Semaphore, settings: Settings,
) -> DownloadOutcome: ...            # writes raw file, returns sha256 + local path + note
def sniff_content_type(raw: bytes, declared: str) -> bool: ...   # pure CPU, inline

# routing.py
def select_loader(file_type: str) -> "LoaderFn": ...   # returns async loader callable

# loaders/pdf.py
async def load_pdf(path: Path, doc_id: str, settings: Settings) -> list[Document]: ...
def _reading_order_sort(blocks: list[Document]) -> list[Document]: ...   # CPU, inline

# loaders/ocr.py
def detect_scanned(path: Path, settings: Settings) -> ScanReport: ...    # CPU -> to_thread
def ocr_pdf(path: Path, pages: list[int], settings: Settings) -> Path: ... # subprocess -> to_thread

# loaders/html.py / office.py / legacy.py
async def load_html(path: Path, doc_id: str, settings: Settings) -> list[Document]: ...
async def load_office(path: Path, doc_id: str, file_type: str, settings: Settings) -> list[Document]: ...
async def convert_legacy(path: Path, settings: Settings) -> tuple[Path, str]: ...  # -> to_thread

# cleaning.py  (pure CPU, inline unless a doc is huge -> to_thread)
def clean(docs: list[Document], settings: Settings) -> list[Document]: ...

# serialize.py
async def write_jsonl(doc_id: str, docs: list[Document], settings: Settings) -> Path: ...

# status.py
async def set_status(session, doc_id: str, status: DocStatus, note: str | None = None) -> None: ...
def build_report(results: list[IngestResult]) -> RunReport: ...

# run.py
async def ingest_one(session, client, row, gates, settings) -> IngestResult: ...
async def main() -> None: ...        # arg parse, gather fan-out, print report
```

`SourceRow`, `DownloadOutcome`, `ScanReport`, `IngestResult`, `RunReport`, `DocStatus` live in
`ingestion/schemas.py`. Each `Document` produced conforms to the metadata expected by the shared
`Chunk` contract downstream: `{doc_id, page_start?, page_end?, anchor?, section_heading?}`.

---

## 5. LCEL composition & the F3 retriever seam

F1 is an ingestion pipeline, not a runtime chain, so it uses LangChain **loaders** rather than an
LCEL runtime graph. The LCEL surface it touches:

- Loaders are the standard LangChain document-loader interface; each `load_*` wraps the sync
  loader's `.load()` inside `anyio.to_thread.run_sync` (the sync loaders have no async twin, so
  the project-wide async rule is satisfied by threading them off the loop rather than calling a
  banned sync method on the event loop).
- The **output contract is the seam to F2/F3**: `data/extracted/{doc_id}.jsonl` of `Document`
  objects with citation-anchoring metadata. F2 reads this JSONL, splits into `Chunk`s, and
  registers them; F3's `BaseRetriever` (the LCEL retriever seam) never sees F1 directly — it sees
  the `chunks` table / vector index that F2 builds from F1's output. F1's only obligation to that
  seam is that **every block already carries the page/anchor/heading needed to build a
  `Citation`**, so no re-parsing is required later in the chain.

No `ainvoke`/`astream` runtime chain is defined in F1; the async rule is honored via awaited
I/O + threaded CPU work.

---

## 6. Error handling

| Failure | Detection | Handling | Status / report |
|---|---|---|---|
| Missing/invalid CSV column | `registry.load_sources` validation | reject row, continue | `failed` + note |
| Duplicate `doc_id` in CSV | pre-run key check | **abort run** before downloads | run aborts loudly |
| Transient HTTP (timeout/5xx) | `tenacity` retry predicate | retry ×3 backoff | stays `registered` until success/exhaust |
| Dead URL (4xx/DNS) | retries exhausted | log, continue | `failed` + listed as dead URL |
| Content-type mismatch | `sniff_content_type` | skip extraction | `failed` + mismatch note |
| Scanned PDF | `detect_scanned` | OCR then reload | `is_scanned=true`, counted in report |
| OCR/`libreoffice`/`unstructured` crash | exception in thread | catch, continue batch | `failed` + tool note |
| Legacy convert failure | `convert_legacy` non-zero exit | mark unsupported | `failed` + unsupported note |
| Version drift (hash≠, label same) | sha256 compare | **abort that doc**, keep old artifacts | `failed` + version-drift note |
| HTML that only links a PDF | link-density heuristic in `html.py` | still register HTML; suggest PDF | note in report suggestions |

All per-document failures are isolated (one bad doc never fails the batch) except the two
loud-abort cases (duplicate `doc_id`, version drift), which are correctness violations.

---

## 7. New Settings keys (central `app.core.settings.Settings`)

```python
# --- ingestion (F1) ---
DATA_DIR: Path = Path("backend/data")
RAW_DIR: Path = Path("backend/data/raw")
EXTRACTED_DIR: Path = Path("backend/data/extracted")
SOURCES_CSV: Path = Path("backend/data/sources.csv")

INGEST_CONCURRENCY: int = 4              # bounded fan-out for extraction workers
INGEST_RATE_LIMIT_PER_SEC: float = 1.0   # polite crawl; Semaphore(1)+sleep
INGEST_MAX_RETRIES: int = 3
INGEST_DOWNLOAD_TIMEOUT_S: float = 60.0

OCR_LANGUAGES: str = "eng+urd"
OCR_MIN_PAGE_TEXT_CHARS: int = 50        # below this + has image => page is scanned
OCR_SCANNED_PAGE_THRESHOLD: float = 0.30 # >30% scanned pages => doc-level is_scanned

CLEAN_HEADER_FOOTER_PAGE_RATIO: float = 0.60  # line on >60% pages => header/footer
CLEAN_MIN_BLOCK_CHARS: int = 20

LIBREOFFICE_BIN: str = "libreoffice"     # legacy .doc/.ppt conversion in ingestion image
```

Every value is read via the one Pydantic `Settings` class (env-overridable); no module-level
constants outside it.

---

## 8. Alembic migration

The `documents` table is **owned by F12**; F1 adds one coordinated migration only for the
ingestion-specific columns/enum F12's baseline may not already carry. If F12 already defines them,
this migration is a no-op guarded by `IF NOT EXISTS`.

`backend/app/db/migrations/versions/xxxx_f1_document_ingestion_fields.py`

```python
def upgrade() -> None:
    document_status = sa.Enum(
        "registered", "downloaded", "extracted", "failed",
        name="document_status",
    )
    document_status.create(op.get_bind(), checkfirst=True)
    # add ingestion columns if not already present in F12 baseline
    op.add_column("documents", sa.Column("status", document_status,
                  nullable=False, server_default="registered"))
    op.add_column("documents", sa.Column("is_scanned", sa.Boolean(),
                  nullable=False, server_default=sa.false()))
    op.add_column("documents", sa.Column("page_count", sa.Integer(), nullable=True))
    op.add_column("documents", sa.Column("sha256", sa.String(length=64), nullable=True))
    op.add_column("documents", sa.Column("downloaded_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("documents", sa.Column("note", sa.Text(), nullable=True))
    op.create_index("ix_documents_status", "documents", ["status"])

def downgrade() -> None:
    op.drop_index("ix_documents_status", table_name="documents")
    for col in ("note", "downloaded_at", "sha256", "page_count", "is_scanned", "status"):
        op.drop_column("documents", col)
    sa.Enum(name="document_status").drop(op.get_bind(), checkfirst=True)
```

(All schema access is async via asyncpg at runtime; the migration itself runs through Alembic per
the project rule that every schema change is a migration. Coordinate the exact column set with
F12 before merge to avoid a double-definition.)

---

## 9. Honoring the shared-context contracts

- **`DocumentMeta`**: F1 populates `doc_id, title, source_org, url, file_type, downloaded_at,
  version_label, is_scanned, page_count, sha256` on the `documents` row — exactly the
  `DocumentMeta` fields.
- **`Chunk` metadata precursor**: extracted `Document.metadata` carries `doc_id`,
  `page_start/page_end` (PDF) or `anchor` (HTML/PPTX/XLSX) and optional `section_heading`, so F2
  can mint `Chunk`s (and later `Citation`s) with zero re-parsing.
- **`Citation` readiness**: because page/anchor/heading are attached at ingestion, a downstream
  `Citation(doc_id, title, section_heading, page_start, page_end, anchor, url, quote)` is fully
  constructible from F1 output + the `documents` row.
- **Async rule**: awaited `httpx`/`aiofiles`/async SQLAlchemy; threaded `unstructured`/`ocrmypdf`/
  `libreoffice`; inline pure-CPU (`sniff_content_type`, reading-order sort, cleaning). Each is
  annotated above with its side of the line.
- **No OpenAI calls** in F1, so the "log token usage + cost on every OpenAI call" rule is
  vacuously satisfied; F1 logs per-doc timing, bytes, and page counts via `structlog` instead.

---

## 10. Test strategy (see tasks.md for the ordered list)

- Fixture files per type under `backend/tests/fixtures/ingestion/` (small, committed).
- Unit tests: routing dispatch, scanned detection thresholds, cleaning (header/footer,
  de-hyphenation, NFC, min-len), content-type sniff, version-drift abort, sha256 dedupe.
- Integration test: `ingest_one` over each fixture reaching `extracted` with page/anchor coverage
  ≥ 95% asserted programmatically.
- CLI test: `--doc`, `--type`, `--force`, `--all` paths with a mocked downloader.
