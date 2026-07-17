"""Idempotent admin seed (design.md §8, AC-5.2)."""

import asyncio

from sqlalchemy import select

from app.core.security import hash_password
from app.core.settings import settings
from app.db.engine import get_sessionmaker
from app.db.enums import UserRole
from app.db.models import User


async def seed_admin() -> None:
    async with get_sessionmaker()() as session:
        existing = await session.scalar(select(User).where(User.email == settings.ADMIN_EMAIL))
        if existing is not None:
            return  # idempotent: admin already provisioned

        session.add(
            User(
                email=settings.ADMIN_EMAIL,
                hashed_password=await hash_password(
                    settings.ADMIN_PASSWORD.get_secret_value(), settings=settings
                ),
                role=UserRole.admin,
                is_active=True,
            )
        )
        await session.commit()


if __name__ == "__main__":
    asyncio.run(seed_admin())
