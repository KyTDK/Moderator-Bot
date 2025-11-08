from __future__ import annotations

import time
from collections import deque
from typing import Deque

__all__ = ["RateTracker"]


class RateTracker:
    """Maintain an event rate over a sliding window."""

    def __init__(self, window: float) -> None:
        self._window = max(1.0, float(window))
        self._events: Deque[float] = deque()

    @property
    def window(self) -> float:
        return self._window

    def record(self) -> None:
        now = time.monotonic()
        self._events.append(now)
        self._prune(now)

    def rate_per_minute(self) -> float:
        now = time.monotonic()
        self._prune(now)
        if not self._events:
            return 0.0
        span = max(1.0, min(self._window, now - self._events[0]))
        return (len(self._events) / span) * 60.0

    def _prune(self, now: float) -> None:
        threshold = now - self._window
        while self._events and self._events[0] < threshold:
            self._events.popleft()
