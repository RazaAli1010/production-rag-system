# F1 — Multi-Format Ingestion Pipeline · tasks.md

**Module:** `backend/app/ingestion/` · **Depends on:** F12 · **Blocks:** F2
Each task is scoped to ≈ ≤ 1 hour and carries its own test criterion. F1 is Phase A, so there is
**no eval-gate task** (the F4 `--compare` gate applies only to Phase B/C enhancements).

Ordering follows the data flow: settings/schemas → registry → downloader → loaders → cleaning →
serialize → status/report → CLI → fixtures → acceptance.

---

### T1 — Settings + F1 schemas
Add the F1 keys from `design.md §7` to the central `Settings` class; create
`ingestion/schemas.py` (`SourceRow`, `DownloadOutcome`, `ScanReport`, `IngestResult`,
`RunReport`, `DocStatus`) and re-export shared contracts.
**Test:** `Settings()` loads all new keys with defaults + env overrides; schema models
round-trip via pydantic; `pytest tests/ingestion/test_settings_schemas.py` green.

### T2 — Alembic migration for documents ingestion fields
Author `xxxx_f1_document_ingestion_fields.py` (status enum + `is_scanned`, `page_count`,
`sha256`, `downloaded_at`, `note`, status index), guarded with `checkfirst`/`IF NOT EXISTS`,
coordinated with F12.
**Test:** `alembic upgrade head` then `downgrade -1` runs clean on a scratch Postgres; columns
present after upgrade, absent after downgrade.

### T3 — Source registry: parse + validate
Implement `registry.load_sources()`: read `sources.csv`, validate required columns and
`file_type` domain, detect duplicate `doc_id` (loud abort per AC-4).
**Test:** valid CSV → list of `SourceRow`; missing column → row rejected with note; duplicate
`doc_id` → run-abort exception raised.

### T4 — Source registry: upsert into documents
Implement `registry.upsert_documents()` (async SQLAlchemy upsert keyed on `doc_id`, new rows
`status=registered`).
**Test:** upsert new rows then re-upsert with changed title → single row per `doc_id`, title
updated, status preserved.

### T5 — Downloader: fetch + write + sha256
Implement `downloader.fetch()` with `httpx.AsyncClient` + `aiofiles` write to
`data/raw/{doc_id}.{ext}`, compute sha256, set `status=downloaded`; sha256 dedupe skip on match.
**Test:** mocked 200 response writes file + returns hash; second call with identical bytes skips
write (AC-10).

### T6 — Downloader: rate limit + retries + sniff
Add `asyncio.Semaphore(1)` + async sleep (≤ 1 req/s), `tenacity` async retry ×3 backoff, and
`sniff_content_type` (inline CPU). Mismatch → `failed`; dead URL after retries → `failed` +
report entry.
**Test:** timed test asserts ≥ 1 s spacing across 2 requests; injected 5xx retried then succeeds;
type mismatch marks `failed`.

### T7 — Loader routing dispatch
Implement `routing.select_loader(file_type)` returning the correct async loader callable per the
`design.md §3` table; unknown type → controlled error.
**Test:** each of `{pdf, html, docx, pptx, xlsx}` maps to expected callable; unknown → raises.

### T8 — PDF fast path + reading-order
Implement `loaders/pdf.load_pdf` (`PyMuPDFLoader`, page-accurate `page_start/page_end`) and
`_reading_order_sort` for two-column pages.
**Test:** digital PDF fixture yields blocks with correct page numbers; two-column fixture emits
left-column blocks before right.

### T9 — Scanned detection + OCR
Implement `loaders/ocr.detect_scanned` (per-page char/image rule, doc-level > 30% threshold) and
`ocr_pdf` (`ocrmypdf` `eng+urd`, selective pages), both via `anyio.to_thread.run_sync`; mixed
PDFs OCR only scanned pages then reload via T8.
**Test:** scanned fixture flagged `is_scanned`, post-OCR text non-empty; mixed fixture keeps
digital pages' original text.

### T10 — HTML loader + anchors + PDF-link heuristic
Implement `loaders/html.load_html` (`UnstructuredHTMLLoader`, `BSHTMLLoader` fallback), strip
nav/boilerplate, capture heading anchors, flag "HTML that only links a PDF" for the report.
**Test:** HTML fixture yields anchors on headings, no nav text; link-only fixture produces a
report suggestion.

### T11 — Office loaders (docx/pptx/xlsx)
Implement `loaders/office.load_office`: DOCX headings → `section_heading`; PPTX slide no →
`anchor`; XLSX sheet → `anchor`, row-wise blocks. Threaded via `to_thread`.
**Test:** each fixture yields expected `anchor`/`section_heading`; XLSX blocks are row-wise with
sheet name.

### T12 — Legacy .doc/.ppt conversion
Implement `loaders/legacy.convert_legacy` via `libreoffice --headless` (threaded); success →
re-route to office loader; failure → `failed` + unsupported note.
**Test:** `.doc` fixture converts and extracts; forced conversion failure marks `failed`
unsupported (mock non-zero exit).

### T13 — Cleaning pipeline
Implement `cleaning.clean`: header/footer strip (> 60% pages), de-hyphenation, whitespace
collapse, drop < 20-char blocks, Unicode NFC, preserve Urdu.
**Test:** synthetic pages with a repeated footer → footer removed; hyphen-split word rejoined;
Urdu string preserved and NFC-normalized; short blocks dropped.

### T14 — JSONL serialization
Implement `serialize.write_jsonl` (aiofiles) to `data/extracted/{doc_id}.jsonl`; blocks carry
`doc_id` + page/anchor + optional `section_heading` (no `chunk_id`/`seq`).
**Test:** file written, each line valid JSON with required metadata keys; no chunk fields present.

### T15 — Status transitions + version drift
Implement `status.set_status` (registered→downloaded→extracted / failed) writing
`page_count/is_scanned/sha256/downloaded_at/note`; enforce version-drift loud abort (hash≠ &&
label unchanged), leaving prior artifacts intact; `--force` respects the same guard.
**Test:** happy path transitions recorded; mutated bytes + same `version_label` → abort, old
JSONL untouched; bumped label → proceeds.

### T16 — Run report
Implement `status.build_report` → totals by status, scanned count, dead URLs, HTML→PDF
suggestions; write `docs/ingestion_report_{ts}.md` + stdout summary.
**Test:** report from a mixed `IngestResult` set contains every required section and correct
counts.

### T17 — CLI orchestration
Implement `run.main()` + `ingest_one`: arg parsing (`--all | --doc | --type` + `--force`),
`asyncio.run(main())`, `asyncio.gather` bounded by `Semaphore(INGEST_CONCURRENCY)`, awaited I/O,
threaded extraction; per-doc failures isolated.
**Test:** `--doc <id>` ingests one; `--type pdf` filters; `--force` re-runs; one failing doc does
not abort the batch (except loud-abort cases).

### T18 — Structlog timing/observability
Emit `structlog` per-doc events (bytes, pages, per-stage ms); confirm no OpenAI/token logging is
attempted (F1 makes no OpenAI calls).
**Test:** log capture shows one structured event per doc with timing + counts.

### T19 — Commit per-type fixtures
Add small committed fixtures under `backend/tests/fixtures/ingestion/`: one each of digital PDF,
scanned PDF, HTML, DOCX, PPTX, XLSX (+ optional legacy `.doc`).
**Test:** fixtures present and loadable; total fixture size kept small (repo-friendly).

### T20 — Acceptance / definition of done
Wire an end-to-end integration test proving the feature-level ACs:
1. one command ingests all 5 types + 1 scanned PDF, **all reach `extracted`**;
2. **≥ 95%** of extracted blocks have page **or** anchor metadata (asserted programmatically);
3. per-type fixtures committed (T19);
4. re-run is idempotent; mutated-fixture-same-label fails loudly;
5. run report produced with all required sections.
**Definition of done:** `pytest tests/ingestion/` green including the end-to-end test, all five
feature-level acceptance criteria asserted, `alembic upgrade head` clean.

---

**No eval gate:** F1 is Phase A foundation. The mandatory F4 `--compare`/delta-report gate begins
at Phase B (F5 Hybrid retrieval) and is not part of this feature's DoD.
