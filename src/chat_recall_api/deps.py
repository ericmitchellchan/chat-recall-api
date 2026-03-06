"""FastAPI dependency injection — database pool and settings."""

from __future__ import annotations

from collections.abc import AsyncGenerator

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from chat_recall_api.config import Settings, get_settings

_pool: AsyncConnectionPool | None = None


async def init_db_pool(database_url: str, min_size: int = 2, max_size: int = 10) -> None:
    """Initialize the global async connection pool."""
    global _pool
    if _pool is not None:
        return
    _pool = AsyncConnectionPool(
        conninfo=database_url, min_size=min_size, max_size=max_size, open=False,
    )
    await _pool.open()


async def close_db_pool() -> None:
    """Close the global connection pool."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def get_db() -> AsyncGenerator[AsyncConnection, None]:
    """FastAPI dependency: yield a connection from the pool."""
    if _pool is None:
        raise RuntimeError("Database pool not initialized. Call init_db_pool() first.")
    async with _pool.connection() as conn:
        yield conn


def get_current_settings() -> Settings:
    """Get application settings."""
    return get_settings()
