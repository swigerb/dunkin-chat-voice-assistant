"""Persistence interfaces for drive-thru simulator state."""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

logger = logging.getLogger(__name__)

try:  # Optional dependency
    import redis.asyncio as aio_redis
except ModuleNotFoundError:  # pragma: no cover - optional import
    aio_redis = None  # type: ignore[assignment]

try:  # Optional dependency
    import asyncpg  # type: ignore[import-untyped]
except ModuleNotFoundError:  # pragma: no cover - optional import
    asyncpg = None  # type: ignore[assignment]


class SimulatorStateStore(Protocol):
    async def load(self) -> list[dict[str, Any]]: ...

    async def persist(self, state: list[dict[str, Any]]) -> None: ...


class InMemorySimulatorStateStore:
    def __init__(self) -> None:
        self._state: list[dict[str, Any]] = []

    async def load(self) -> list[dict[str, Any]]:
        return list(self._state)

    async def persist(self, state: list[dict[str, Any]]) -> None:
        self._state = list(state)


class RedisSimulatorStateStore:
    def __init__(self, url: str, key: str = "drive_thru:state") -> None:
        if aio_redis is None:  # pragma: no cover - runtime guard
            raise RuntimeError("redis package is required for RedisSimulatorStateStore")
        self._client = aio_redis.from_url(url, decode_responses=True)
        self._key = key

    async def load(self) -> list[dict[str, Any]]:
        raw = await self._client.get(self._key)
        if not raw:
            return []
        try:
            return json.loads(raw)
        except json.JSONDecodeError:  # pragma: no cover - defensive
            logger.warning("Invalid Redis simulator payload; resetting state")
            return []

    async def persist(self, state: list[dict[str, Any]]) -> None:
        await self._client.set(self._key, json.dumps(state))


class PostgresSimulatorStateStore:
    def __init__(self, dsn: str, table_name: str = "drive_thru_state") -> None:
        if asyncpg is None:  # pragma: no cover - runtime guard
            raise RuntimeError("asyncpg is required for PostgresSimulatorStateStore")
        self._dsn = dsn
        self._table = table_name
        self._pool: asyncpg.Pool | None = None

    async def _ensure_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            self._pool = await asyncpg.create_pool(dsn=self._dsn)
            async with self._pool.acquire() as conn:
                await conn.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {self._table} (
                        id INT PRIMARY KEY DEFAULT 1,
                        state JSONB NOT NULL,
                        updated_at TIMESTAMPTZ DEFAULT NOW()
                    )
                    """
                )
        return self._pool

    async def load(self) -> list[dict[str, Any]]:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(f"SELECT state FROM {self._table} WHERE id = 1")
            if row is None:
                return []
            return row["state"] or []

    async def persist(self, state: list[dict[str, Any]]) -> None:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                f"""
                INSERT INTO {self._table} (id, state) VALUES (1, $1)
                ON CONFLICT (id) DO UPDATE SET state = EXCLUDED.state, updated_at = NOW()
                """,
                state,
            )
