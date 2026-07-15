# CLAUDE.md — CampusRAG

Production RAG assistant for **University of the Punjab (PU) regulations + HEC policies**.
Citation-first Q&A: every answer cites exact document, section, and page. Low-confidence
retrieval → **refusal, not hallucination**. Users are Pakistani students on mobile, asking
messy typo-ridden Urdu/English code-switched questions ("probation se kaise nikalta hoon").

## Core philosophy (do not violate)

1. Build the **simplest working RAG first (F3)**, then add ONE enhancement at a time.
2. Every enhancement (F5, F6, F9, F17, and any augmentation/generation feature) ends with a
   **mandatory eval gate**: run the F4 harness,
   `--compare` the previous phase label, and commit a delta report to `docs/eval_results/`.
   A feature is NOT done until its delta table exists.
3. Every enhancement is **toggleable via a config/request flag** so A/B and prod rollback
   always work.

## Fixed tech stack (do NOT re-litigate in specs or code)

- **Orchestration:** LangChain (LCEL chains, loaders, splitters, retrievers, callbacks).
- **Backend:** FastAPI, Python 3.11+, Pydantic v2, fully async.
- **Frontend:** React 18 + Vite + TypeScript, Tailwind, TanStack Query.
- **Vector DB:** Pinecone serverless (namespaces `pu` / `hec`). pgvector is deliberately NOT used.
- **Relational DB:** PostgreSQL (Supabase/Neon free tier).
- **ORM/migrations:** SQLAlchemy 2.0 async + Alembic.
- **Cache / rate-limit:** Redis (Upstash).
- **Embeddings:** OpenAI `text-embedding-3-small` (1536-dim) via `langchain-openai`.
- **LLM:** `gpt-4o-mini` primary; `gpt-4o` deep mode.
- **Parsing:** `unstructured` (+ `pymupdf` fast path, `ocrmypdf`/Tesseract for scans).
- **Sparse retrieval:** BM25 (`rank-bm25`, in-process).
- **Reranker:** `cross-encoder/ms-marco-MiniLM-L-6-v2` (sentence-transformers, CPU).
- **Evals:** RAGAS + custom hit@k.
- **Auth:** OAuth2 password flow + JWT (`pyjwt`, `passlib[bcrypt]`); ALL auth/authz state in Postgres.
- **Observability:** Langfuse + `structlog` + Postgres request logs.
- **Deploy/CI:** Docker multi-stage, GitHub Actions, Render (API) + Vercel (UI), Locust load tests.

**v2 stretch only — note but do NOT implement:** LangGraph agentic routing, LlamaIndex
comparison, pgvector, second-LLM fallback, social login, email verification, webhook Telegram,
long-term cross-session memory.

## Async/await mandate (project-wide, CI-enforced)

Every I/O path is async end-to-end. Inside `backend/app/` the **sync twins are BANNED**
(ruff/grep CI check): no `invoke`, `embed_documents`, blocking `requests`, or sync `redis`.

- HTTP → `httpx.AsyncClient`; files → `aiofiles`; Redis → `redis.asyncio`; DB → async
  SQLAlchemy + asyncpg.
- LangChain only via its async surface: `ainvoke` / `astream` / `astream_events` /
  `aembed_documents` / `aembed_query`.
- Fan-out: `asyncio.gather` bounded by `asyncio.Semaphore`. Write-behind: `asyncio.create_task`.
- **CPU-bound work off the loop** via `anyio.to_thread.run_sync` / `run_in_executor`: bcrypt,
  cross-encoder scoring, OCR/`unstructured` parsing, BM25 pickle load.
- Cheap pure-CPU (tiktoken counting, cosine matmul on the cache matrix) may run inline. Every
  spec must state which side of that line its work falls on.

## Repo structure (monorepo)

```
campus-rag/
├── backend/app/
│   ├── api/            # routers: ask, auth, documents, admin, health
│   ├── core/           # settings, security, logging, exceptions
│   ├── db/             # SQLAlchemy models, session, Alembic migrations (F12)
│   ├── ingestion/      # F1   indexing/  # F2   rag/  # F3 + F5–F6
│   ├── memory/         # F17: sessions, token accounting, summarizer, stage events
│   ├── caching/        # F9   auth/  # F10   evals/  # F4   observability/  # F13
│   ├── data/           # sources.csv, raw files, extracted JSONL, bm25.pkl
│   └── tests/
├── frontend/           # F14      docker/
└── docs/
    ├── specs/<feature>/    # requirements.md, design.md, tasks.md
    └── eval_results/       # one delta report per enhancement feature
```

## Build order & dependency graph (build in this order)

```
PHASE A: F12 Persistence → F1 Ingestion → F2 Indexing → F3 Baseline RAG → F4 Eval harness
PHASE B — Retrieval enhancement (each eval-gated): F5 Hybrid → F6 Reranking   # retrieval track ENDS here
PHASE B2 — Augmentation & Generation (each eval-gated; features + labels TBD, spec later)
PHASE C: F9 Semantic cache → F10 Auth → F17 Session memory → F11 API hardening → F13 Observability
PHASE D: F14 Frontend → F15 Deploy/CI/load-test → F16 Telegram bot (optional)
```

Retrieval enhancement stops at F6 reranking; the former F7 (query rewrite) and F8 (compression)
are dropped — the next enhancement phase (B2) targets augmentation & generation instead. Within
Phase B the build order IS the measurement order: each feature's "before" = the previous
feature's "after". Build F12 FIRST — it blocks everything.

## Canonical data contracts (Pydantic — the cross-feature interface)

`DocumentMeta`, `Chunk`, `RetrievedChunk` (dense/sparse/fused/rerank scores),
`Citation` (quote ≤ 25 words), `ChatMessage`, `MemoryContext`, `StageEvent`, `AnswerResponse`.

- **IDs:** chunk `{doc_id}:{chunk_seq}`; doc = slug+year (`hec-plagiarism-policy-2021`).
- **Postgres tables (owned by F12, referenced by name elsewhere):** `users`, `api_keys`,
  `refresh_tokens`, `login_attempts`, `documents`, `chunks`, `sessions`, `messages`,
  `request_logs`, `cache_entries`, `eval_runs`, `eval_results`.
- **`refresh_tokens` IS the JWT blacklist**: a jti is valid iff a row exists with
  `revoked_at IS NULL` and not expired.

## SSE contract (produced by F3, extended by F17, served by F11, rendered by F14)

Ordered event types on `/api/ask`:
`stage` (repeated, live pipeline status, Claude-style) → `token` (repeated) → `citations` →
`meta` (final `AnswerResponse` sans answer text) → `done` | `error`.
All producers are async generators (`astream_events` → FastAPI `StreamingResponse`); **no
stage may block the event loop**. Even the F3 baseline emits `stage` events so F17/F14 need
no contract change — later features only add stages.

## Pipeline order (F11, sequence diagram required)

validate → auth → rate limit → **load memory + summarize-if-flagged + condense-follow-up (F17)**
→ cache lookup (F9) → hybrid retrieve (F5) → rerank (F6) → refusal gate →
generate (F3 chain, `MemoryContext` in prompt) → cache write → persist assistant message
(F17, write-behind) → log (F13). Flags checked at each seam; each seam emits paired `stage`
events via the single F17 emitter (`app/memory/stages.py`).

## Settings (one Pydantic `Settings` class — every config value goes here)

- Secrets/URLs: `OPENAI_API_KEY`, `PINECONE_API_KEY`, `PINECONE_INDEX`, `DATABASE_URL`
  (Postgres, asyncpg), `REDIS_URL`, `JWT_SECRET`, `LANGFUSE_*`.
- Feature flags: `ENABLE_HYBRID`, `ENABLE_RERANK`, `ENABLE_CACHE`, `ENABLE_MEMORY`.
- Memory: `MEMORY_TOKEN_BUDGET=50_000`, `MEMORY_WINDOW_PAIRS=5` (sliding window),
  `MEMORY_KEEP_LAST_PAIRS=2` (shrunken window once over budget),
  `MEMORY_SUMMARIZE_EVERY_PAIRS=3` (lazy-summary batch), `MEMORY_SUMMARY_MAX_TOKENS=600`.

## Session memory rule (F17 — sliding window + summarization hybrid)

The prompt NEVER carries the full transcript. Prompt context per turn is always one of:

- **≤5 pairs so far:** all pairs verbatim + current question.
- **>5 pairs, under 50k:** rolling summary (of older pairs) + last 5 pairs + current question.
- **over 50k budget:** rolling summary + last 2 pairs + current question (`summarized=true`).

Pairs that slide out are folded into a rolling `gpt-4o-mini` summary, lazily and in batches
(one call per 3 slid-out pairs). History is context for dialogue coherence ONLY and is
explicitly **non-citable** — a `[n]` may only point at retrieved chunks. F17 condenses
follow-ups into a **standalone question**; retrieval AND the F9 cache key both use it, so
follow-ups never poison the cache. Memory reads are O(window), not O(transcript). Per-session
`asyncio.Lock` serializes concurrent asks → second request returns 409 `session_busy`.

## Eval-gate label sequence (fixed, used with `--compare`)

`baseline` → `f5-hybrid-after` → `f6-rerank-after` → `f9-cache-after` → `f17-memory-after`
(latency/cost suites only for the last two). Every README benchmark row maps to a label; every
label maps to a git SHA + index manifest so all numbers are reproducible.

## Commands

```bash
# DB / migrations
make db-up && make migrate           # local docker Postgres + Redis, alembic upgrade head

# Ingestion (F1) — asyncio.run entrypoint
python -m app.ingestion.run --all | --doc <id> | --type pdf --force

# Indexing (F2) — strategy change forces --wipe
python -m app.indexing.run --strategy fixed|structure --namespace pu|hec|all --wipe

# Evals (F4) — the eval-gate artifact
python -m app.evals.run --suite retrieval|ragas|refusal|latency|all \
    --flags hybrid=on,rerank=off,... --label "f5-hybrid-after"
python -m app.evals.run --compare baseline    # per-metric, per-slice deltas

# Cache (F9)
python -m app.caching.run --flush

# Local full stack
docker compose up
```

## Non-negotiable rules

- Don't change fixed stack decisions; respect each feature's "Out of scope" notes.
- Every new config value lives in the central `Settings` class.
- **Every schema change is an Alembic migration** — all DB access is async (asyncpg).
- Every metric mentioned in a spec must actually be logged (`request_logs` + Langfuse).
- Every enhancement must be toggleable; the F4 harness always runs `skip_cache=true` and
  `session_id=None` so retrieval metrics stay comparable across labels.
- Log token usage + estimated cost on every OpenAI call via one central `estimate_cost()`.
- Prompt rule (unchanged everywhere): answer ONLY from retrieved context, cite claims as
  `[n]`, insufficient context → refuse, respond in the question's language, quotes ≤ 25 words.
- Raw query text is stored ONLY hashed in `request_logs`; chat `messages` store raw text by
  design (user-visible product data vs telemetry — keep the privacy test green).
- Tests: pytest + pytest-asyncio; each feature defines its own acceptance tests, which are the
  definition of done.

## Spec generation (per feature, in `docs/specs/<feature>/`)

Produce `requirements.md` (user stories + EARS acceptance criteria), `design.md` (module
layout, LCEL composition, data-flow, error handling, new Settings keys, Alembic migrations,
how it honors Shared Context contracts + the F3 retriever seam), and `tasks.md` (ordered
tasks ≤ ~1h each, ending with acceptance criteria as DoD). Phase B/C features MUST end with
the eval-gate task.
