# F1 — Multi-Format Ingestion Pipeline · requirements.md

**Module:** `backend/app/ingestion/`
**Phase:** A (foundation) · **Depends on:** F12 (Persistence) · **Blocks:** F2 (Indexing)
**Status of eval gate:** N/A — F1 is a Phase A foundation feature, not a Phase B/C enhancement, so no F4 eval-gate task applies.

---

## 1. Overview

F1 turns a curated list of source URLs (PU regulations, PU Calendar, HEC policies, fee tables,
notifications) into **clean, versioned, citation-anchored text** ready for chunking in F2. It
must handle five first-class file types — PDF (digital + scanned), HTML, DOCX, PPTX, XLSX — via
LangChain document loaders plus `unstructured`, with an `ocrmypdf` fallback for scans.

Every extracted block must carry enough metadata (`doc_id`, page or anchor, optional
`section_heading`) for a downstream `Citation` to point a student at an exact page/section. The
pipeline is async end-to-end, idempotent, and versioned: an unannounced content change fails
loudly rather than silently re-indexing.

The unit of persistence F1 owns is a row in the `documents` table (schema owned by F12) plus a
serialized `data/extracted/{doc_id}.jsonl` of LangChain `Document` objects. F1 does **not** chunk,
embed, or index — those are F2.

---

## 2. User stories

- **US-1 (Corpus maintainer):** As the person maintaining the corpus, I want to declare every
  source in a single `sources.csv` so that one command ingests the whole set reproducibly.
- **US-2 (Corpus maintainer):** As the maintainer, I want polite, rate-limited, retrying
  downloads so that I never hammer a government/university site and transient failures self-heal.
- **US-3 (Corpus maintainer):** As the maintainer, I want each file type routed to the correct
  loader automatically so I don't hand-configure parsing per document.
- **US-4 (Corpus maintainer):** As the maintainer, I want scanned PDFs detected and OCR'd
  (English + Urdu) so that image-only regulations become searchable text.
- **US-5 (Downstream F2 developer):** As the indexing author, I want extracted blocks to carry
  page/anchor/heading metadata so that citations resolve to an exact location.
- **US-6 (Downstream F2 developer):** As the indexing author, I want a stable JSONL contract
  (`Document` objects with `doc_id`, page/anchor) so I can build chunks without re-parsing.
- **US-7 (Corpus maintainer):** As the maintainer, I want a run report of what succeeded,
  failed, and needs attention so I can fix the corpus without reading logs line by line.
- **US-8 (Corpus maintainer):** As the maintainer, I want re-runs to be idempotent and a changed
  source hash to fail loudly so I never silently ship a mutated document under an old version.
- **US-9 (Corpus maintainer):** As the maintainer, I want targeted runs (`--doc`, `--type`,
  `--force`) so I can re-ingest one document or one format without touching the rest.
- **US-10 (Ops):** As an operator, I want the CPU/subprocess-heavy work (OCR, `unstructured`)
  kept off the event loop so a large batch run stays responsive and cancellable.

---

## 3. EARS acceptance criteria

### 3.1 Source registry
- **AC-1 (Ubiquitous):** The system shall read `data/sources.csv` with columns
  `doc_id, title, source_org, url, file_type, version_label, notes`.
- **AC-2 (Event-driven):** When an ingestion run starts, the system shall upsert each CSV row
  into the `documents` table keyed by `doc_id`, setting `status = registered` for new rows.
- **AC-3 (Unwanted):** If a CSV row is missing a required column or declares a `file_type`
  outside `{pdf, html, docx, pptx, xlsx}`, then the system shall reject that row, mark it
  `failed` with a note, and continue processing the remaining rows.
- **AC-4 (Unwanted):** If two CSV rows share the same `doc_id`, then the system shall abort the
  run before any download with a duplicate-key error naming the offending `doc_id`.

### 3.2 Downloader
- **AC-5 (Event-driven):** When a registered document is downloaded, the system shall fetch it
  with `httpx.AsyncClient`, write bytes to `data/raw/{doc_id}.{ext}` via `aiofiles`, compute a
  sha256, and set `status = downloaded`.
- **AC-6 (State-driven):** While downloading a batch, the system shall enforce ≤ 1 request/second
  using an `asyncio.Semaphore(1)` plus async sleep (polite crawling).
- **AC-7 (Event-driven):** When a download fails transiently (timeout/5xx/connection error), the
  system shall retry up to 3 times with exponential backoff via `tenacity` async mode.
- **AC-8 (Unwanted):** If the response content-type does not match the declared `file_type` after
  sniffing, then the system shall mark the document `failed` with a mismatch note and continue.
- **AC-9 (Unwanted):** If a URL is dead (4xx/DNS/permanent error after retries), then the system
  shall log it, mark the document `failed`, list it in the run report, and continue the batch.
- **AC-10 (Event-driven):** When re-downloading a document whose bytes are byte-identical to the
  stored copy (sha256 match), the system shall skip the write and reuse the cached file.

### 3.3 Loader routing
- **AC-11 (Ubiquitous):** The system shall route each document to a loader by `file_type` per the
  routing table in `design.md`, and normalize every output to LangChain `Document` objects with
  metadata `{doc_id, page|anchor, section_heading?}`.
- **AC-12 (Event-driven — PDF fast path):** When a digital PDF is loaded, the system shall use
  `PyMuPDFLoader` and populate page-accurate `page_start`/`page_end` on each block.
- **AC-13 (State-driven — scanned):** While a PDF is classified scanned (a page has < 50 chars of
  extractable text but contains images, and > 30% of pages meet this), the system shall set
  document-level `is_scanned = true`, run `ocrmypdf` (Tesseract `eng+urd`), then re-load via the
  PDF fast path.
- **AC-14 (Event-driven — mixed PDF):** When a PDF is partly scanned, the system shall OCR only
  the scanned pages and preserve the digital text of the rest.
- **AC-15 (Event-driven — HTML):** When an HTML page is loaded, the system shall use
  `UnstructuredHTMLLoader` (falling back to `BSHTMLLoader`), strip nav/boilerplate, and capture
  heading anchors into `anchor` (page fields remain `None`).
- **AC-16 (Event-driven — DOCX):** When a DOCX is loaded, the system shall use
  `UnstructuredWordDocumentLoader` and preserve headings as `section_heading`.
- **AC-17 (Event-driven — PPTX):** When a PPTX is loaded, the system shall use
  `UnstructuredPowerPointLoader` and record the slide number in `anchor`.
- **AC-18 (Event-driven — XLSX):** When an XLSX is loaded, the system shall use
  `UnstructuredExcelLoader`, emit row-wise text blocks, and record the sheet name in `anchor`.
- **AC-19 (Event-driven — two-column):** When a two-column PDF page (e.g. PU Calendar) is loaded,
  the system shall emit blocks in reading order (left column fully before right).
- **AC-20 (Event-driven — legacy):** When a legacy `.doc`/`.ppt` is encountered, the system shall
  convert it to `.docx`/`.pptx` via `libreoffice --headless` inside the ingestion image, then
  route normally; if conversion fails, mark `failed` with an unsupported note.

### 3.4 Cleaning
- **AC-21 (Event-driven):** When cleaning a paginated document, the system shall strip lines that
  repeat as headers/footers on > 60% of pages.
- **AC-22 (Ubiquitous):** The system shall de-hyphenate line-break splits, collapse redundant
  whitespace, and drop blocks shorter than 20 characters after cleaning.
- **AC-23 (Ubiquitous):** The system shall preserve Urdu text and normalize all text to
  Unicode NFC.

### 3.5 Serialization & status
- **AC-24 (Event-driven):** When extraction completes for a document, the system shall write all
  cleaned `Document` objects to `data/extracted/{doc_id}.jsonl` and set `status = extracted`,
  recording `page_count`, `is_scanned`, `sha256`, and `downloaded_at` on the `documents` row.
- **AC-25 (Ubiquitous):** The system shall use chunk-independent `Document` metadata only; it
  shall not assign `chunk_id`/`seq` (those belong to F2).
- **AC-26 (Event-driven):** When any stage fails for a document, the system shall set
  `status = failed` with a human-readable note and continue the batch.

### 3.6 Idempotency & versioning
- **AC-27 (Unwanted):** If a re-downloaded source's sha256 differs from the stored hash while its
  `version_label` is unchanged, then the system shall abort that document with a loud
  version-drift error and leave the prior `extracted` artifacts intact.
- **AC-28 (Event-driven):** When `--force` is passed, the system shall re-download and re-extract
  regardless of cached hash, but shall still enforce AC-27 unless `version_label` was bumped.

### 3.7 CLI & concurrency
- **AC-29 (Ubiquitous):** The system shall expose
  `python -m app.ingestion.run [--all | --doc <id> | --type <ft>] [--force]` with entrypoint
  `asyncio.run(main())`.
- **AC-30 (Ubiquitous):** The system shall await all HTTP and DB-status writes, and run
  CPU/subprocess-heavy extraction (`unstructured`, `ocrmypdf`, `libreoffice`) off the event loop
  via `anyio.to_thread.run_sync` with bounded worker concurrency.
- **AC-31 (Ubiquitous):** The system shall log token usage only where an OpenAI call occurs; F1
  makes no OpenAI calls, so it shall log per-document timing and byte/page counts instead.

### 3.8 Observability of the run
- **AC-32 (Event-driven):** When a run finishes, the system shall print/write a report
  summarizing per-document status, counts by status, scanned-PDF count, dead URLs, and any HTML
  pages that merely link to a PDF (with a suggestion to register the PDF).

---

## 4. Acceptance criteria (feature-level definition of done)

1. **One command** ingests a fixture set containing all 5 file types **plus 1 scanned PDF**, and
   every fixture reaches `status = extracted`.
2. **≥ 95%** of extracted blocks carry page **or** anchor metadata.
3. Committed **unit-test fixtures per file type** (pdf digital, pdf scanned, html, docx, pptx,
   xlsx) live under `backend/tests/fixtures/ingestion/`.
4. Re-running the same command is idempotent; a mutated fixture with an unchanged `version_label`
   fails loudly (AC-27).
5. The run report (AC-32) is produced and lists at least: totals by status, scanned count,
   dead URLs, HTML-links-to-PDF suggestions.

---

## 5. Out of scope (do not implement here)

- Chunking, token windows, and `chunk_id`/`seq` assignment → **F2**.
- Embeddings / vector upsert / BM25 index → **F2**.
- Crawling/spidering — only the explicit `sources.csv` URL list is fetched.
- Audio/video ingestion.
- Perfect fidelity of merged-cell / multi-row-header XLSX (linearize row-wise; good-enough only).
