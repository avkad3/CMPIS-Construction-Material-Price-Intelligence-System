"""
database.py
-----------
Async database configuration for CMPIS (demo-data backend).

Uses SQLite via aiosqlite for local development. The connection string is
the ONLY thing that needs to change to move to PostgreSQL later — nothing
in models.py, analytics.py, service.py, or main.py references SQLite
directly, so the migration is a one-line change plus an Alembic migration
pass in production.
"""

from __future__ import annotations

import logging
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    AsyncEngine,
    create_async_engine,
    async_sessionmaker,
)
from sqlalchemy.orm import DeclarativeBase

logger = logging.getLogger("cmpis.database")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# SQLite file lives alongside the backend code during development.
# To move to Postgres later: swap this for
#   postgresql+asyncpg://user:password@host:port/dbname
# No other file in this project needs to change.
DATABASE_URL = "sqlite+aiosqlite:///./construction_prices.db"


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models (SQLAlchemy 2.0 style)."""
    pass


# ---------------------------------------------------------------------------
# Engine & Session Factory
# ---------------------------------------------------------------------------
# pool_pre_ping avoids using stale/dead connections; harmless no-op on SQLite,
# essential once this points at a real Postgres instance.
engine: AsyncEngine = create_async_engine(
    DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
    future=True,
)

# expire_on_commit=False keeps ORM attributes readable after commit, which
# matters because analytics.py reads attributes on objects fetched earlier
# in the same request.
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


# ---------------------------------------------------------------------------
# Foreign key enforcement (SQLite disables this by default per-connection)
# ---------------------------------------------------------------------------
if DATABASE_URL.startswith("sqlite"):
    from sqlalchemy import event

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, connection_record) -> None:
        """Ensure ON DELETE CASCADE (Product -> PriceHistory) actually works."""
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


# ---------------------------------------------------------------------------
# Lifecycle helpers
# ---------------------------------------------------------------------------
async def init_db() -> None:
    """
    Create all tables if they don't exist yet.
    Safe to call on every app startup — it's a no-op if tables already exist.
    In a real production deployment this would be replaced by Alembic
    migrations, but is fine for a demo/dev backend.
    """
    # Import models here (not at module top) to avoid circular imports,
    # since models.py imports Base from this module.
    from . import models  # noqa: F401  (import needed to register tables on Base.metadata)

    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Database schema verified/created successfully.")
    except Exception:
        logger.exception("Failed to initialize database schema.")
        raise


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI dependency that yields a database session per-request and
    guarantees it is closed afterwards, regardless of success or failure.

    Usage in a route:
        async def endpoint(db: AsyncSession = Depends(get_db)):
            ...
    """
    session = AsyncSessionLocal()
    try:
        yield session
        await session.commit()
    except Exception:
        logger.exception("Request-scoped session error — rolling back.")
        await session.rollback()
        raise
    finally:
        await session.close()


async def dispose_engine() -> None:
    """Gracefully release the connection pool on app shutdown."""
    await engine.dispose()
    logger.info("Database engine connection pool disposed.")