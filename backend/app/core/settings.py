"""Central Pydantic Settings class.

Per CLAUDE.md, every config value in the app lives in this one class. F12 only adds the
DB-related keys (design.md §6) plus the seed-admin credentials; other features' keys
(OPENAI_API_KEY, feature flags, memory tuning, etc.) are added by the features that own them so
F12's boot/tests don't require unrelated real credentials.
"""

from pathlib import Path
from typing import Literal

from pydantic import EmailStr, PostgresDsn, RedisDsn, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Database (F12) ---
    DATABASE_URL: PostgresDsn
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 2
    DB_POOL_TIMEOUT: int = 30
    DB_POOL_RECYCLE: int = 1800
    DB_STATEMENT_CACHE_SIZE: int = 0
    DB_ECHO: bool = False

    # --- Seed admin (F12) ---
    ADMIN_EMAIL: EmailStr
    ADMIN_PASSWORD: SecretStr

    # --- Ingestion (F1) ---
    # Paths are relative to the `backend/` cwd (how `make migrate`, alembic, and pytest all run)
    # and match the repo-structure + .gitignore convention of `backend/app/data/`.
    DATA_DIR: Path = Path("app/data")
    RAW_DIR: Path = Path("app/data/raw")
    EXTRACTED_DIR: Path = Path("app/data/extracted")
    SOURCES_CSV: Path = Path("app/data/sources.csv")

    INGEST_CONCURRENCY: int = 4  # bounded fan-out for extraction workers
    INGEST_RATE_LIMIT_PER_SEC: float = 1.0  # polite crawl; Semaphore(1)+sleep
    INGEST_MAX_RETRIES: int = 3
    INGEST_DOWNLOAD_TIMEOUT_S: float = 60.0

    OCR_LANGUAGES: str = "eng+urd"
    OCR_MIN_PAGE_TEXT_CHARS: int = 50  # below this + has image => page is scanned
    OCR_SCANNED_PAGE_THRESHOLD: float = 0.30  # >30% scanned pages => doc-level is_scanned

    CLEAN_HEADER_FOOTER_PAGE_RATIO: float = 0.60  # line on >60% pages => header/footer
    CLEAN_MIN_BLOCK_CHARS: int = 20

    LIBREOFFICE_BIN: str = "libreoffice"  # legacy .doc/.ppt conversion in ingestion image

    # repo-root `docs/` (CLAUDE.md repo structure), relative to the `backend/` cwd (AC-32).
    INGESTION_REPORT_DIR: Path = Path("../docs")

    # --- Chunking & Indexing (F2) ---
    OPENAI_API_KEY: SecretStr
    EMBED_MODEL: str = "text-embedding-3-small"
    EMBED_DIM: int = 1536
    EMBED_BATCH_SIZE: int = 100
    EMBED_CONCURRENCY: int = 4
    EMBED_MAX_RETRIES: int = 5
    EMBED_MAX_CHUNK_TOKENS: int = 8000

    PINECONE_API_KEY: SecretStr
    PINECONE_INDEX: str
    PINECONE_UPSERT_CONCURRENCY: int = 4
    PINECONE_METADATA_MAX_BYTES: int = 40_000

    INDEXING_STRATEGY: Literal["fixed", "structure"] = "fixed"
    FIXED_CHUNK_TOKENS: int = 500
    FIXED_CHUNK_OVERLAP: int = 50
    STRUCTURE_MAX_SECTION_TOKENS: int = 800
    STRUCTURE_CLAUSE_PATTERNS: list[str] = [
        r"^\d+(\.\d+)*[.)]\s", r"^[A-Z][A-Z \-]{6,}$", r"Regulation No\.",
    ]

    BM25_PATH: Path = Path("app/data/bm25.pkl")
    INDEX_MANIFEST_PATH: Path = Path("app/data/index_manifest.json")

    # --- Hybrid search (F5) ---
    ENABLE_HYBRID: bool = False  # prod/request toggle; False ≡ F3 dense-only path (AC-11)
    # eval-only explicit override; wins over ENABLE_HYBRID so dense_only|bm25_only|hybrid A/Bs run
    # under the F4 harness with no F4 code change (AC-13). None => derive mode from ENABLE_HYBRID.
    RETRIEVAL_MODE: Literal["dense_only", "bm25_only", "hybrid"] | None = None
    HYBRID_DENSE_TOP_K: int = 20  # dense candidates before fusion (raised from RETRIEVAL_K, AC-5)
    HYBRID_SPARSE_TOP_K: int = 20  # BM25 candidates before fusion (AC-3)
    HYBRID_FUSED_TOP_K: int = 12  # fused pool cap exposed to F6 rerank (AC-9)
    HYBRID_RRF_K: int = 60  # RRF constant; fused_score = Σ 1/(60 + rank) (AC-7)
    # BM25_PATH (above, F2) is reused verbatim for the sparse index — NOT redefined.

    # --- Reranking (F6) ---
    ENABLE_RERANK: bool = False  # prod/request toggle; False ≡ F5 path (AC-17)
    RERANK_MODEL: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"  # fixed stack (AC-1)
    # PINNED cpu: HuggingFaceCrossEncoder auto-selects CUDA > MPS > CPU, so without this it would
    # leave CPU on a dev/CI box with a GPU or Apple silicon (AC-1).
    RERANK_DEVICE: str = "cpu"
    RERANK_TOP_N: int = 5  # kept after rerank → count handed to generation (AC-6)
    RERANK_CANDIDATE_K: int = 12  # pool size fed to rerank; matches HYBRID_FUSED_TOP_K
    # Calibration (AC-10/AC-11): sigmoid the raw logits into [0, 1]. ms-marco-MiniLM-L-6-v2's head
    # emits a single unbounded logit, so the default is True; the T6 activation sanity check
    # confirms (or flips) this against the real model so a double-sigmoid can't corrupt the gate.
    RERANK_APPLY_SIGMOID: bool = True
    # Calibrated refusal gate (AC-12/AC-13): refuse if max_rerank_score < this. TUNED (not guessed)
    # on the 75-record eval set at the F6 gate: out-of-corpus queries all score ≤ 0.005 (the English
    # cross-encoder finds no relevant chunk), so a Youden-optimal 0.01 refuses 100% of out-of-corpus
    # while still answering ~86% of in-corpus. A higher value (e.g. 0.5) over-refuses because the
    # prose-trained model scores in-corpus *code-switched* queries near 0. See
    # docs/eval_results/f6-rerank-after-vs-f5-hybrid-after.md.
    REFUSAL_RERANK_THRESHOLD: float = 0.01
    # HYBRID_FUSED_TOP_K (above, F5) is reused as the hybrid rerank input pool — NOT redefined.

    # --- Query rewrite (F7) ---
    ENABLE_QUERY_REWRITE: bool = False  # prod/request toggle; False ≡ f6-rerank-after path (AC-15)
    # The rewrite LLM — the project PRIMARY model, gpt-4o-mini; gpt-4o "deep mode" is NOT used for
    # rewrite (AC-1/AC-20). Explicit Settings value so it is overridable and asserted in tests.
    REWRITE_MODEL: str = "gpt-4o-mini"
    REWRITE_TEMPERATURE: float = 0.0  # deterministic rewrite (AC-1)
    REWRITE_MAX_TOKENS: int = 200  # JSON output cap (AC-1)
    REWRITE_NUM_VARIANTS: int = 2  # multi-query paraphrases emitted alongside `normalized` (AC-2)
    REWRITE_RRF_K: int = 60  # union-across-queries RRF constant; merged = Σ 1/(60 + rank) (AC-6)
    REWRITE_MERGED_TOP_K: int = 12  # merged pool cap fed to rerank (= RERANK_CANDIDATE_K)
    REWRITE_FANOUT_CONCURRENCY: int = 3  # Semaphore bound over [normalized, v1, v2] fan-out (AC-5)
    # rewrite call timeout → raw-query fallback; guards the ≤600ms p50 budget (AC-8/AC-10)
    REWRITE_TIMEOUT_S: float = 5.0
    # RERANK_TOP_N / RERANK_CANDIDATE_K / HYBRID_* are reused by the fan-out, NOT redefined.

    # --- Context compression (F8) ---
    ENABLE_COMPRESSION: bool = False  # prod/request toggle; False ≡ f7-rewrite-after gen path (AC-16)
    COMPRESSION_SCORE_FLOOR: float = 0.25  # drop reranked chunks below this calibrated score (AC-1)
    COMPRESSION_MIN_CHUNKS: int = 2  # never leave a non-refused query fewer than this many (AC-2)
    COMPRESSION_TOKEN_BUDGET: int = 2200  # greedy-fill budget; overflow chunk sentence-trimmed (AC-6/7)
    COMPRESSION_DEDUPE_JACCARD: float = 0.7  # 5-gram Jaccard above this drops the lower-scored dup (AC-4)
    COMPRESSION_DEDUPE_NGRAM: int = 5  # word-level n-gram size for the dedupe similarity (AC-4)
    # RERANK_MODEL / RERANK_DEVICE reused for sentence scoring, NOT redefined.

    # --- Semantic cache (F9) ---
    ENABLE_CACHE: bool = False  # prod/request toggle; False ≡ f8-compression-after path (AC-30)
    # Hot tier. None => Redis disabled entirely (AC-4): the cache still works Postgres-only, so
    # local dev and CI need no Redis. `docker/docker-compose.yml` already ships redis:7 locally.
    REDIS_URL: RedisDsn | None = None
    CACHE_REDIS_TTL_S: int = 86_400  # 24h exact-match TTL; the Postgres tier has no TTL
    CACHE_REDIS_TIMEOUT_S: float = 0.25  # a slow hot tier must not out-cost the miss it saves
    CACHE_KEY_PREFIX: str = "campusrag:cache:"  # also the SCAN pattern for --flush (AC-20)
    # Accept rule (AC-7): cosine >= threshold AND no discriminative-token disagreement. Both
    # signals must clear. TUNED at T7 against tests/fixtures/cache/adversarial.jsonl with real
    # text-embedding-3-small vectors, not guessed — same discipline as REFUSAL_RERANK_THRESHOLD.
    #
    # 0.86 (not the specced 0.95): NOTHING in the adversarial set reaches 0.95 — at that threshold
    # the semantic tier never fires at all. The sets also OVERLAP on cosine (worst adversarial pair
    # 15(3) vs 15(4) = 0.930 > best true paraphrase 0.912), so cosine alone cannot separate them;
    # the discriminative veto (keys.discriminators) is what does the separating, and it rejects
    # 7/10 adversarial pairs with 0 false vetoes. With the veto applied, 0.86 keeps 2/8 paraphrases
    # at zero collisions with a 0.032 margin over the nearest surviving adversarial pair
    # ("semester registration deadline" vs "semester withdrawal deadline", 0.828). 0.85 would keep
    # 3/8 but leave only 0.022; the margin is the scarcer resource, so 0.86 it is.
    #
    # That margin is THIN and the honest reason ENABLE_CACHE ships default-off (the F7 precedent).
    # The Redis exact-match tier carries the real value; the semantic tier is opt-in. Re-run T7's
    # calibration before widening this — see docs/eval_results/f9-cache-after.md.
    CACHE_SIMILARITY_THRESHOLD: float = 0.86
    # Tokens that change WHICH document answers a question rather than how it is phrased. A cache
    # match is vetoed when the two queries disagree on any of these (years and section ids are
    # detected structurally in keys.py). A lexical Jaccard floor was specced here and DROPPED: with
    # the veto in place its optimal value measured 0.0 — it rejected nothing the veto did not
    # already reject, and no floor that admits real paraphrases (0.125-0.667 Jaccard) excludes the
    # adversarial pairs (0.4-0.67).
    #
    # DEGREE LEVELS ONLY — the issuing bodies `pu`/`hec` were listed here and removed after the T15
    # live run: they cost a real false veto ("...to sit PU exams" yields {pu} while "...at Punjab
    # University" yields {}, so two phrasings of ONE question stopped matching), and they buy
    # nothing — the PU-vs-HEC plagiarism pair they were meant to catch sits at 0.740 cosine, which
    # CACHE_SIMILARITY_THRESHOLD rejects on its own. Verified: dropping them leaves 0 collisions and
    # the same 2/8 paraphrases on the committed adversarial set. An entry here must earn its keep by
    # catching a pair that CLEARS the cosine floor.
    CACHE_DISCRIMINATIVE_TERMS: list[str] = [
        "bs", "bsc", "ms", "msc", "mphil", "phd", "adp", "ba", "ma",
        "undergraduate", "postgraduate", "graduate",
    ]
    # Brute-force ceiling: 10k × 1536 float32 = 61 MB resident and a sub-ms matmul — the
    # justification for having no vector index at all (pgvector stays banned; Pinecone is the
    # vector store). At the cap, writes evict the least-recently-hit entry (AC-18).
    # ponytail: single-process matrix. If the API ever runs >1 replica each holds its own — revisit.
    CACHE_MAX_ENTRIES: int = 10_000
    # EMBED_MODEL / EMBED_DIM (F2) are reused for the query vector — NOT redefined.

    # --- Session memory (F17) ---
    ENABLE_MEMORY: bool = False  # prod/request toggle; False ≡ f9-cache-after single-turn (AC-33)
    MEMORY_TOKEN_BUDGET: int = 50_000  # hard cap; crossing it shrinks the window (AC-20)
    MEMORY_WINDOW_PAIRS: int = 5  # verbatim window under budget (AC-18/19)
    MEMORY_KEEP_LAST_PAIRS: int = 2  # shrunken window once over budget (AC-20)
    MEMORY_SUMMARIZE_EVERY_PAIRS: int = 3  # lazy-batch trigger for the summariser (AC-23)
    MEMORY_SUMMARY_MAX_TOKENS: int = 600  # summary output cap (AC-23)
    MEMORY_SUMMARY_MODEL: str = "gpt-4o-mini"  # project primary; NOT gpt-4o deep mode (AC-23)
    MEMORY_SUMMARY_TEMPERATURE: float = 0.0  # deterministic summary
    MEMORY_SUMMARY_TIMEOUT_S: float = 8.0  # summariser timeout → window-only fallback (AC-27)
    MEMORY_SESSION_TITLE_MAX_CHARS: int = 60  # auto-title cap (AC-2)
    MEMORY_ANON_MAX_MESSAGES: int = 30  # anonymous session message cap (AC-7)
    MEMORY_ANON_TTL_DAYS: int = 7  # anonymous inactivity TTL, pruned by the F12 job (AC-7)

    # --- API hardening (F11) ---
    ENABLE_RATE_LIMIT: bool = True  # prod toggle; False ≡ F17 route, never 429 (AC-12/22)
    CORS_ALLOW_ORIGINS: list[str] = []  # exact-origin allowlist; empty ⇒ no cross-origin (AC-15)
    REQUEST_TIMEOUT_S: float = 30.0  # server-side ask timeout → SSE error / 504 (AC-17)
    GZIP_MIN_BYTES: int = 500  # gzip threshold; SSE frames stay below it, so streaming is unaffected
    HISTORY_PAGE_SIZE: int = 50  # GET /api/history page (AC-5)
    RATE_LIMIT_WINDOW_S: int = 60  # fixed-window size for the Redis limiter (AC-8)
    # F11 plumbs `deep=true` to this model by handing the pipeline a settings copy with LLM_MODEL
    # overridden — no baseline.py change, since build_llm(settings) reads LLM_MODEL (AC-3).
    LLM_DEEP_MODEL: str = "gpt-4o"

    # --- RAG baseline chain (F3) ---
    LLM_MODEL: str = "gpt-4o-mini"  # gpt-4o is F3's "deep mode" toggle, not wired until later
    LLM_MAX_RETRIES: int = 2  # 429/5xx retry budget (AC-21)
    RETRIEVAL_K: int = 5  # default k; still an explicit answer()/astream() param
    RETRIEVAL_NAMESPACES: list[str] = ["pu", "hec"]  # fan-out targets when namespace=None (AC-4)
    REFUSAL_DENSE_THRESHOLD: float = 0.25  # cosine; pre-LLM refusal gate (AC-6)
    REFUSAL_SUGGESTION_COUNT: int = 3  # "you might check" citations on refusal (AC-7)
    MAX_QUERY_TOKENS: int = 200  # truncate-and-warn guard (AC-13)
    CITATION_QUOTE_MAX_WORDS: int = 25  # AC-16
    DISCLAIMER_TEXT: str = (
        "This assistant summarizes official PU/HEC documents but is not a substitute for the "
        "official regulation text. Always verify against the cited source before acting."
    )

    # --- Langfuse observability (F3) ---
    # Optional: absent in CI/local dev means observability.langfuse_handler() returns None (no
    # callback attached) rather than failing boot — Langfuse is not a hard requirement (AC-25
    # is satisfied whenever these are configured, not unconditionally).
    LANGFUSE_PUBLIC_KEY: SecretStr | None = None
    LANGFUSE_SECRET_KEY: SecretStr | None = None
    LANGFUSE_HOST: str = "https://cloud.langfuse.com"

    # --- Evaluation harness (F4) ---
    # Paths relative to the `backend/` cwd (how alembic/pytest/`python -m app...` all run).
    EVAL_DATASET_PATH: Path = Path("app/data/evals/qa_dataset.jsonl")  # git-versioned QA set (AC-1)
    EVAL_RESULTS_DIR: Path = Path("../docs/eval_results")  # repo-root docs dir
    EVAL_JUDGE_MODEL: str = "gpt-4o-mini"  # RAGAS judge (AC-9)
    EVAL_HIT_KS: list[int] = [1, 3, 5]  # hit@k cutoffs (AC-5)
    EVAL_RETRIEVAL_K: int = 5  # k passed to retrieve() — must be >= max(EVAL_HIT_KS)
    EVAL_RAGAS_METRICS: list[str] = [
        "faithfulness", "answer_relevancy", "context_precision", "context_recall",
    ]
    EVAL_RAGAS_JUDGE_MULTIPLIER: float = 4.0  # judge prompts per record, for cost preview (AC-11)
    # Cap RAGAS evaluate()'s internal judge concurrency. The library default (16) storms a
    # rate-limited OpenAI tier into a retry-backoff stall (80+ hung connections); 4 matches the
    # generation fan-out and completes reliably. Same for every label, so deltas stay comparable.
    EVAL_RAGAS_MAX_WORKERS: int = 4
    EVAL_LATENCY_REQUESTS: int = 100  # AC-16
    # F9: cap the DISTINCT questions the latency suite samples, so `EVAL_LATENCY_REQUESTS` over a
    # smaller pool produces a deliberate repeat rate (30 requests / 15 unique = 50% repeats). None
    # = every answerable record, i.e. the pre-F9 behaviour, so earlier labels are unaffected.
    #
    # Needed because the repeat rate was otherwise an ACCIDENT of (N mod dataset size): the f9 gate
    # runs at f8's N=30 against 63 answerable records, where `answerable[i % 63]` for i in 0..29
    # yields 30 DISTINCT questions and a 0% hit rate — the cache would have measured nothing.
    # Making the ratio explicit also makes it identical across both labels, which is the only way
    # the delta compares two pipelines rather than two workloads.
    EVAL_LATENCY_UNIQUE_QUESTIONS: int | None = None
    EVAL_LATENCY_ENDPOINT: str | None = None  # F11 /api/ask URL; None => in-process astream (AC-17)
    EVAL_CONCURRENCY: int = 4  # bounded async fan-out over records (Semaphore)
    EVAL_DATASET_MIN: int = 60  # 60-80 record range (AC-2)
    EVAL_DATASET_MAX: int = 80
    EVAL_QUOTA_CODE_SWITCHED: int = 15  # AC-3
    EVAL_QUOTA_OUT_OF_CORPUS: int = 10  # AC-3

    # --- Auth (F10) ---
    JWT_SECRET: SecretStr  # required, no default
    JWT_ALGORITHM: str = "HS256"
    JWT_LEEWAY_S: int = 30  # clock skew, exp only
    ACCESS_TOKEN_TTL_MIN: int = 15
    REFRESH_TOKEN_TTL_DAYS: int = 7
    BCRYPT_ROUNDS: int = 12
    AUTH_EMAIL_DOMAIN_ALLOWLIST: list[str] = []  # empty => any domain
    # Keyed on email, not IP: IP-keying lets one hostile client lock out every account behind a
    # university/carrier NAT.
    LOGIN_MAX_FAILURES: int = 10
    LOGIN_LOCKOUT_WINDOW_MIN: int = 15
    # Resolved here by rate_tier(); enforced by F11.
    RATE_LIMIT_ANON_PER_MIN: int = 5
    RATE_LIMIT_STUDENT_PER_MIN: int = 20
    RATE_LIMIT_ADMIN_PER_MIN: int = 60
    RATE_LIMIT_API_KEY_PER_MIN: int = 30


settings = Settings()
