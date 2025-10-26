from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator


class ConcurrencyPool:
    """Lazily create keyed semaphores for tier-aware throttling."""

    __slots__ = ("_lock", "_semaphores")

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._semaphores: dict[tuple[str, str], tuple[asyncio.Semaphore, int]] = {}

    async def _ensure(self, key: str, resource: str, limit: int) -> asyncio.Semaphore:
        normalized = (key, resource)
        async with self._lock:
            entry = self._semaphores.get(normalized)
            if entry is not None:
                semaphore, current_limit = entry
                if current_limit == limit:
                    return semaphore
            semaphore = asyncio.Semaphore(max(1, limit))
            self._semaphores[normalized] = (semaphore, limit)
            return semaphore

    @asynccontextmanager
    async def limit(
        self,
        key: str | int | None,
        resource: str,
        limit: int | None,
    ) -> AsyncIterator[None]:
        if not limit or limit <= 0:
            yield
            return
        semaphore = await self._ensure(str(key or "global"), resource, limit)
        await semaphore.acquire()
        try:
            yield
        finally:
            semaphore.release()


concurrency_pool = ConcurrencyPool()

__all__ = ["ConcurrencyPool", "concurrency_pool"]
