"""Async SQLAlchemy engine and session factory."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings
from app.database.models import Base
from app.utils.logger import logger

engine_kwargs = {"echo": False}
if not settings.database_url.startswith("sqlite"):
    engine_kwargs.update(
        {
            "pool_pre_ping": True,
            "pool_size": 5,
            "max_overflow": 10,
            "pool_recycle": 1800,
        }
    )

engine = create_async_engine(settings.database_url, **engine_kwargs)

SessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def init_db() -> None:
    """Create all tables if they don't yet exist."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        columns = await conn.run_sync(
            lambda sync_conn: {
                column["name"]
                for column in inspect(sync_conn).get_columns("monitored_accounts")
            }
        )
        if "instagram_id" not in columns:
            await conn.execute(
                text("ALTER TABLE monitored_accounts ADD COLUMN instagram_id VARCHAR(64)")
            )
            logger.info("Added monitored_accounts.instagram_id column")
        hl_columns = await conn.run_sync(
            lambda sync_conn: {
                column["name"]
                for column in inspect(sync_conn).get_columns("stored_highlights")
            }
        )
        if "tracked" not in hl_columns:
            await conn.execute(
                text(
                    "ALTER TABLE stored_highlights "
                    "ADD COLUMN tracked BOOLEAN NOT NULL DEFAULT TRUE"
                )
            )
            logger.info("Added stored_highlights.tracked column")
    logger.info("Database schema verified")


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    """Yield a session and ensure commit/rollback semantics."""
    session = SessionLocal()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def dispose_engine() -> None:
    await engine.dispose()
