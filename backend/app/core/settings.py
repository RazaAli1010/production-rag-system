"""Central Pydantic Settings class.

Per CLAUDE.md, every config value in the app lives in this one class. F12 only adds the
DB-related keys (design.md §6) plus the seed-admin credentials; other features' keys
(OPENAI_API_KEY, feature flags, memory tuning, etc.) are added by the features that own them so
F12's boot/tests don't require unrelated real credentials.
"""

from pathlib import Path
from typing import Literal

from pydantic import EmailStr, PostgresDsn, SecretStr
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
    EVAL_LATENCY_REQUESTS: int = 100  # AC-16
    EVAL_LATENCY_ENDPOINT: str | None = None  # F11 /api/ask URL; None => in-process astream (AC-17)
    EVAL_CONCURRENCY: int = 4  # bounded async fan-out over records (Semaphore)
    EVAL_DATASET_MIN: int = 60  # 60-80 record range (AC-2)
    EVAL_DATASET_MAX: int = 80
    EVAL_QUOTA_CODE_SWITCHED: int = 15  # AC-3
    EVAL_QUOTA_OUT_OF_CORPUS: int = 10  # AC-3


settings = Settings()
