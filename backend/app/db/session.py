"""`get_session` — the one async FastAPI dependency every router/chain shares (design.md §4).

Commits on clean exit, rolls back and re-raises on any exception, and always returns the
connection to the pool via the `async with` block (AC-1.3).
"""

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import get_sessionmaker


async def get_session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
