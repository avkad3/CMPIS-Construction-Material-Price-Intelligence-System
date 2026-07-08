"""
database.py
-----------
Database configuration for CMPIS.

Contains ONLY:

- SQLAlchemy Base
- Async Engine
- Async Session Factory
- get_db()
- get_session()
- init_db()
- dispose_engine()

Models live in models.py.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

logger = logging.getLogger("cmpis.database")

# ---------------------------------------------------------------------
# DATABASE
# ---------------------------------------------------------------------

DATABASE_URL = os.getenv(
    "CMPIS_DATABASE_URL",
    "sqlite+aiosqlite:///./cmpis.db",
)

# ---------------------------------------------------------------------
# BASE
# ---------------------------------------------------------------------


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------
# ENGINE
# ---------------------------------------------------------------------

engine: AsyncEngine = create_async_engine(
    DATABASE_URL,
    echo=False,
    future=True,
)

# ---------------------------------------------------------------------
# SESSION FACTORY
# ---------------------------------------------------------------------

AsyncSessionFactory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)

# Backward compatibility
AsyncSessionLocal = AsyncSessionFactory

# ---------------------------------------------------------------------
# FASTAPI DEPENDENCY
# ---------------------------------------------------------------------


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionFactory() as session:
        try:
            yield session
        finally:
            await session.close()


# ---------------------------------------------------------------------
# CONTEXT MANAGER
# ---------------------------------------------------------------------


@asynccontextmanager
async def get_session():
    session = AsyncSessionFactory()

    try:
        yield session
        await session.commit()

    except Exception:
        await session.rollback()
        raise

    finally:
        await session.close()


# ---------------------------------------------------------------------
# INITIALIZE DATABASE
# ---------------------------------------------------------------------


async def init_db():
    """
    Import models BEFORE create_all()
    so SQLAlchemy knows every table.
    """

    import demo_data.models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    logger.info("Database initialized.")


# ---------------------------------------------------------------------
# SHUTDOWN
# ---------------------------------------------------------------------


async def dispose_engine():
    await engine.dispose()