from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _serialise(value: Any) -> Any:
    """Ensure metric payloads are JSON-serialisable."""
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, (list, tuple, set)):
        return [_serialise(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _serialise(val) for key, val in value.items()}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


@dataclass(slots=True)
class ModerationMetric:
    """Represents a rich moderation event captured for analytics."""

    event_type: str
    content_type: str
    guild_id: int | None = None
    channel_id: int | None = None
    user_id: int | None = None
    message_id: int | None = None
    was_flagged: bool = False
    flags_count: int = 0
    primary_reason: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    scan_duration_ms: int | None = None
    scanner: str | None = None
    source: str | None = None
    reference: str | None = None
    occurred_at: datetime = field(default_factory=_utc_now)

    def to_mysql_params(self) -> tuple[Any, ...]:
        """Return a tuple aligned with moderation_metrics column order."""
        occurred = self.occurred_at.astimezone(timezone.utc).replace(tzinfo=None)
        payload = json.dumps(_serialise(self.details or {}), ensure_ascii=False)
        return (
            occurred,
            self.guild_id,
            self.channel_id,
            self.user_id,
            self.message_id,
            self.content_type,
            self.event_type,
            bool(self.was_flagged),
            int(self.flags_count or 0),
            self.primary_reason,
            payload,
            self.scan_duration_ms,
            self.scanner,
            self.source,
            self.reference,
        )


__all__ = ["ModerationMetric"]
