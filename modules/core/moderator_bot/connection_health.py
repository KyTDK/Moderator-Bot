from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import time
from typing import Deque


@dataclass(slots=True)
class GatewayHealthSnapshot:
    """Summary details for a burst of disconnect events."""

    disconnect_count: int
    window_seconds: float
    first_disconnect_age: float
    last_disconnect_age: float


class GatewayHealthMonitor:
    """Track gateway disconnect frequency and expose snapshots when alert-worthy."""

    def __init__(
        self,
        *,
        threshold: int = 4,
        window_seconds: float = 180.0,
        cooldown_seconds: float = 900.0,
    ) -> None:
        self._threshold = max(1, threshold)
        self._window_seconds = max(1.0, window_seconds)
        self._cooldown_seconds = max(30.0, cooldown_seconds)
        self._disconnects: Deque[float] = deque()
        self._last_alert_monotonic: float = 0.0

    def record_disconnect(self) -> GatewayHealthSnapshot | None:
        """Record a gateway disconnect; return a snapshot if threshold/cooldown satisfied."""
        now = time.monotonic()
        self._disconnects.append(now)
        self._trim(now)

        if len(self._disconnects) < self._threshold:
            return None

        if now - self._last_alert_monotonic < self._cooldown_seconds:
            return None

        first = self._disconnects[0]
        snapshot = GatewayHealthSnapshot(
            disconnect_count=len(self._disconnects),
            window_seconds=self._window_seconds,
            first_disconnect_age=now - first,
            last_disconnect_age=0.0,
        )
        self._last_alert_monotonic = now
        return snapshot

    def _trim(self, now: float) -> None:
        window = self._window_seconds
        while self._disconnects and now - self._disconnects[0] > window:
            self._disconnects.popleft()


__all__ = ["GatewayHealthMonitor", "GatewayHealthSnapshot"]
