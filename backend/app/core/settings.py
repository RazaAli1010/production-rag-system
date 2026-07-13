"""Central Pydantic Settings class.

Per CLAUDE.md, every config value in the app lives in this one class. F12 only adds the
DB-related keys (design.md §6) plus the seed-admin credentials; other features' keys
(OPENAI_API_KEY, feature flags, memory tuning, etc.) are added by the features that own them so
F12's boot/tests don't require unrelated real credentials.
"""

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


settings = Settings()
