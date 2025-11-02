from __future__ import annotations

import time
from collections import OrderedDict


class TenorToggleCache:
    """LRU cache for Tenor toggle lookups with TTL semantics."""

    def __init__(self, *, ttl: float, max_items: int):
        self._ttl = float(ttl)
        self._max_items = int(max_items)
        self._entries: "OrderedDict[int, tuple[float, bool]]" = OrderedDict()

    def get(self, guild_id: int) -> bool | None:
        entry = self._entries.get(guild_id)
        if not entry:
            return None
        expires_at, value = entry
        if expires_at <= time.monotonic():
            self._entries.pop(guild_id, None)
            return None
        refreshed_expiry = time.monotonic() + self._ttl
        self._entries[guild_id] = (refreshed_expiry, value)
        self._entries.move_to_end(guild_id)
        return value

    def set(self, guild_id: int, value: bool) -> None:
        expires_at = time.monotonic() + self._ttl
        self._entries[guild_id] = (expires_at, bool(value))
        self._entries.move_to_end(guild_id)
        while len(self._entries) > self._max_items:
            self._entries.popitem(last=False)


__all__ = ["TenorToggleCache"]
