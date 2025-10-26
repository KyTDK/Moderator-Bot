from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, NamedTuple


class CacheReservation(NamedTuple):
    verdict: dict[str, Any] | None
    waiter: asyncio.Future | None
    token: object | None


@dataclass(slots=True)
class _CacheEntry:
    timestamp: float
    verdict: dict[str, Any]


class ContentVerdictCache:
    """Short-lived cache for NSFW verdicts with in-flight de-duplication."""

    __slots__ = ("_ttl", "_max", "_lock", "_entries", "_inflight")

    def __init__(self, *, ttl: float = 900.0, max_size: int = 1024) -> None:
        self._ttl = ttl
        self._max = max_size
        self._lock = asyncio.Lock()
        self._entries: "OrderedDict[str, _CacheEntry]" = OrderedDict()
        self._inflight: dict[str, asyncio.Future] = {}

    def _prune_locked(self) -> None:
        if not self._entries:
            return
        now = time.monotonic()
        expired = [
            key for key, entry in self._entries.items() if now - entry.timestamp >= self._ttl
        ]
        for key in expired:
            self._entries.pop(key, None)
        while len(self._entries) > self._max:
            self._entries.popitem(last=False)

    async def claim(self, key: str) -> CacheReservation:
        async with self._lock:
            self._prune_locked()
            entry = self._entries.get(key)
            if entry is not None:
                self._entries.move_to_end(key)
                return CacheReservation(verdict=entry.verdict, waiter=None, token=None)

            if key in self._inflight:
                return CacheReservation(
                    verdict=None,
                    waiter=self._inflight[key],
                    token=None,
                )

            token = object()
            fut = asyncio.get_running_loop().create_future()
            self._inflight[key] = fut
            return CacheReservation(verdict=None, waiter=None, token=token)

    async def resolve(self, key: str, token: object | None, verdict: dict[str, Any]) -> None:
        if token is None:
            await self._notify_waiters(key, verdict)
            return
        async with self._lock:
            fut = self._inflight.pop(key, None)
            self._entries[key] = _CacheEntry(timestamp=time.monotonic(), verdict=verdict)
            self._entries.move_to_end(key)
            self._prune_locked()
        if fut is not None and not fut.done():
            fut.set_result(verdict)

    async def fail(self, key: str, exc: Exception) -> None:
        async with self._lock:
            fut = self._inflight.pop(key, None)
        if fut is not None and not fut.done():
            fut.set_exception(exc)

    async def _notify_waiters(self, key: str, verdict: dict[str, Any]) -> None:
        async with self._lock:
            fut = self._inflight.pop(key, None)
            if verdict is not None:
                self._entries[key] = _CacheEntry(timestamp=time.monotonic(), verdict=verdict)
                self._entries.move_to_end(key)
                self._prune_locked()
        if fut is not None and not fut.done():
            fut.set_result(verdict)


verdict_cache = ContentVerdictCache()

__all__ = ["ContentVerdictCache", "verdict_cache", "CacheReservation"]
