"""asyncpg pool + helpers.

Every runner and the FastAPI app uses one shared pool per process via `get_pool()`.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import asyncpg

from src.core.config import settings


_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn=settings.pg_dsn,
            min_size=1,
            max_size=8,
            command_timeout=30,
        )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


@asynccontextmanager
async def conn() -> AsyncIterator[asyncpg.Connection]:
    pool = await get_pool()
    async with pool.acquire() as connection:
        yield connection


async def heartbeat(app: str, status: str = "ok", detail: str | None = None) -> None:
    """Upsert into runs so the dashboard /health and top-bar indicator are live."""
    async with conn() as c:
        await c.execute(
            """
            INSERT INTO runs (app, last_beat, status, detail)
            VALUES ($1, now(), $2, $3)
            ON CONFLICT (app) DO UPDATE
              SET last_beat = EXCLUDED.last_beat,
                  status    = EXCLUDED.status,
                  detail    = EXCLUDED.detail
            """,
            app, status, detail,
        )


async def fetch(query: str, *args: Any) -> list[asyncpg.Record]:
    async with conn() as c:
        return await c.fetch(query, *args)


async def fetchrow(query: str, *args: Any) -> asyncpg.Record | None:
    async with conn() as c:
        return await c.fetchrow(query, *args)


async def execute(query: str, *args: Any) -> str:
    async with conn() as c:
        return await c.execute(query, *args)
