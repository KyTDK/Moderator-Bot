from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

from modules.metrics import get_media_metrics_summary


@dataclass(frozen=True, slots=True)
class MediaProcessingRate:
    """Represents an observed processing rate for a specific media type."""

    content_type: str
    scans: int
    per_minute: float

    def format_console(self, window_minutes: float) -> str:
        window = max(window_minutes, 1 / 60)
        return (
            f"{self.content_type}: {self.per_minute:.2f}/min "
            f"over {window:.1f}m ({self.scans} scans)"
        )


class MediaRateCalculator:
    """Calculate media processing rates over a fixed lookback window."""

    def __init__(self, *, lookback: timedelta | None = None) -> None:
        self._lookback = lookback or timedelta(minutes=15)

    @property
    def lookback(self) -> timedelta:
        return self._lookback

    @property
    def window_minutes(self) -> float:
        return max(self._lookback.total_seconds() / 60.0, 1 / 60)

    async def compute_rates(self) -> list[MediaProcessingRate]:
        since = datetime.now(timezone.utc) - self._lookback
        summary = await get_media_metrics_summary(since=since)

        window_minutes = self.window_minutes
        rates: list[MediaProcessingRate] = []
        for bucket in summary:
            scans = int(bucket.get("scans", 0))
            if scans <= 0:
                continue
            content_type = str(bucket.get("content_type") or "unknown")
            per_minute = scans / window_minutes
            rates.append(
                MediaProcessingRate(
                    content_type=content_type,
                    scans=scans,
                    per_minute=per_minute,
                )
            )
        rates.sort(key=lambda payload: payload.per_minute, reverse=True)
        return rates

    @staticmethod
    def format_rates_for_embed(
        rates: Iterable[MediaProcessingRate],
        window_minutes: float,
    ) -> str:
        rate_list = list(rates)
        if not rate_list:
            return "No media processed in the selected window."
        lines = [
            f"• {rate.content_type}: {rate.per_minute:.2f}/min ({rate.scans} scans)"
            for rate in rate_list
        ]
        return "\n".join(lines)


__all__ = ["MediaProcessingRate", "MediaRateCalculator"]
