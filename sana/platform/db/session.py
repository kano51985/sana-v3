"""Async SQLAlchemy engine and request-scoped session factories."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def create_database_engine(
    database_url: str,
    *,
    echo: bool = False,
    pool_pre_ping: bool = True,
) -> AsyncEngine:
    if not database_url.startswith(("postgresql+asyncpg://", "postgresql://")):
        raise ValueError("Sana requires a PostgreSQL database URL")
    async_url = database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return create_async_engine(
        async_url,
        echo=echo,
        pool_pre_ping=pool_pre_ping,
    )


def create_session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
