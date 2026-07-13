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


settings = Settings()
