"""SQLAlchemy engine/session wiring.

One async engine for the app and API; a sync engine only for Alembic and the
CLI so migrations and reindex have no event loop requirement.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from metaedit.config import Settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def init_engine(settings: Settings) -> AsyncEngine:
    """Create the process-wide async engine (idempotent)."""
    global _engine, _session_factory
    if _engine is None:
        _engine = create_async_engine(
            settings.database_url,
            echo=settings.db_echo,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_pre_ping=True,
        )
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        msg = "init_engine() must be called before get_session_factory()"
        raise RuntimeError(msg)
    return _session_factory


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a session with commit/rollback semantics."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def sync_engine(database_url: str) -> Engine:
    """A psycopg3 sync engine for Alembic and CLI commands."""
    url = database_url.replace("postgresql+psycopg_async://", "postgresql+psycopg://")
    if "+asyncpg" in url:
        url = url.replace("+asyncpg", "+psycopg")
    return create_engine(url, pool_pre_ping=True)


@contextmanager
def sync_connection(database_url: str) -> Iterator[Engine]:
    engine = sync_engine(database_url)
    try:
        yield engine
    finally:
        engine.dispose()
