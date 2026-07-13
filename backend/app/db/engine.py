"""Async engine & sessionmaker — the one pooled asyncpg engine for the process (design.md §4).

Built once (via lru_cache) and reused for the process lifetime (AC-1.1). No CPU-bound work
happens here, so no `anyio.to_thread`/executor offload is introduced (AC-1.6) — every path below
is `async`/`await` over asyncpg.
"""

from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.settings import settings


def _connect_args() -> dict:
    # AC-1.4 — pgbouncer/session-pooler (e.g. Supabase) needs prepared statements off.
    if settings.DB_STATEMENT_CACHE_SIZE == 0:
        return {"statement_cache_size": 0, "prepared_statement_cache_size": 0}
    return {}


@lru_cache
def get_engine() -> AsyncEngine:
    return create_async_engine(
        str(settings.DATABASE_URL),
        pool_size=settings.DB_POOL_SIZE,  # free-tier-safe cap, default <=5 (AC-1.2)
        max_overflow=settings.DB_MAX_OVERFLOW,
        pool_timeout=settings.DB_POOL_TIMEOUT,
        pool_recycle=settings.DB_POOL_RECYCLE,
        pool_pre_ping=True,  # drop stale free-tier connections
        echo=settings.DB_ECHO,
        connect_args=_connect_args(),
    )


@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False, autoflush=False)
