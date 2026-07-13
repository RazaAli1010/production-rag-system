"""Idempotent admin seed (design.md §8, AC-5.2).

Until F10's passlib helper exists, password hashing happens here via a local bcrypt call run
through `anyio.to_thread.run_sync` (CPU-bound work off the event loop, per the project-wide
async rule) — this file will delegate to F10's helper once it lands.
"""

import asyncio

import anyio
from passlib.context import CryptContext
from sqlalchemy import select

from app.core.settings import settings
from app.db.engine import get_sessionmaker
from app.db.enums import UserRole
from app.db.models import User

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def _hash_password(password: str) -> str:
    return _pwd_context.hash(password)


async def seed_admin() -> None:
    async with get_sessionmaker()() as session:
        existing = await session.scalar(select(User).where(User.email == settings.ADMIN_EMAIL))
        if existing is not None:
            return  # idempotent: admin already provisioned

        hashed_password = await anyio.to_thread.run_sync(
            _hash_password, settings.ADMIN_PASSWORD.get_secret_value()
        )
        session.add(
            User(
                email=settings.ADMIN_EMAIL,
                hashed_password=hashed_password,
                role=UserRole.admin,
                is_active=True,
            )
        )
        await session.commit()


if __name__ == "__main__":
    asyncio.run(seed_admin())
