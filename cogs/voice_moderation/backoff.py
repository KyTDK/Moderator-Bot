from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Dict, Tuple


@dataclass(slots=True)
class _BackoffEntry:
    until: float
    attempts: int


class VoiceConnectBackoff:
    """Simple exponential backoff tracker per guild/channel combo."""

    def __init__(self, *, base_seconds: float = 30.0, max_seconds: float = 600.0) -> None:
        self._base = max(5.0, base_seconds)
        self._max = max(self._base, max_seconds)
        self._entries: Dict[Tuple[int, int], _BackoffEntry] = {}

    def record_failure(self, guild_id: int, channel_id: int) -> float:
        key = (guild_id, channel_id)
        entry = self._entries.get(key)
        attempts = 1 if entry is None else entry.attempts + 1
        delay = min(self._base * (2 ** (attempts - 1)), self._max)
        self._entries[key] = _BackoffEntry(
            until=time.monotonic() + delay,
            attempts=attempts,
        )
        return delay

    def clear(self, guild_id: int, channel_id: int) -> None:
        self._entries.pop((guild_id, channel_id), None)

    def remaining(self, guild_id: int, channel_id: int) -> float:
        entry = self._entries.get((guild_id, channel_id))
        if not entry:
            return 0.0
        remaining = entry.until - time.monotonic()
        if remaining <= 0:
            self._entries.pop((guild_id, channel_id), None)
            return 0.0
        return remaining


VOICE_BACKOFF = VoiceConnectBackoff()

__all__ = ["VoiceConnectBackoff", "VOICE_BACKOFF"]
