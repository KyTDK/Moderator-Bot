from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any


def ensure_utc(value: datetime | None) -> datetime:
    result = value or datetime.now(timezone.utc)
    if result.tzinfo is None:
        return result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def ensure_naive(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=None)


def normalise_since(value: date | datetime | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).date()
    return value


def parse_iso_datetime(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def json_dumps(payload: Any | None) -> str:
    if payload is None:
        return ""
    try:
        return json.dumps(payload, ensure_ascii=False)
    except TypeError:
        return json.dumps(str(payload))


def json_loads(raw: str | None) -> Any:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def compute_average(total: int, count: int) -> float:
    return float(total) / float(count) if count > 0 else 0.0


__all__ = [
    "coerce_int",
    "compute_average",
    "ensure_naive",
    "ensure_utc",
    "json_dumps",
    "json_loads",
    "normalise_since",
    "parse_iso_datetime",
]
