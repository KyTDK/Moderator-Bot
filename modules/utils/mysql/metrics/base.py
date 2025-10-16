from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Sequence

from ..metrics_schema import METRIC_AGGREGATE_COLUMNS


def ensure_utc(dt: datetime | None) -> datetime:
    value = dt or datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def ensure_naive(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=None)


def decode_json_map(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="ignore")
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
    else:
        data = raw
    if not isinstance(data, dict):
        return {}
    result: dict[str, Any] = {}
    for key, value in data.items():
        result[str(key)] = value
    return result


def encode_json_map(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def normalise_since(value: date | datetime | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).date()
    return value


def _normalise_json_text(raw: Any) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, (bytes, bytearray)):
        return raw.decode("utf-8", errors="ignore")
    if isinstance(raw, str):
        return raw
    return json.dumps(raw, ensure_ascii=False)


@dataclass(slots=True)
class MetricUpdate:
    status: str
    was_flagged: bool
    flags_increment: int
    bytes_increment: int
    duration_increment: int
    reference: str | None
    detail_json: str
    store_last_details: bool
    occurred_at: datetime | None


AGGREGATE_COLUMN_NAMES = tuple(column.name for column in METRIC_AGGREGATE_COLUMNS)


def _default_aggregate_values() -> dict[str, int]:
    return {
        column.name: int(column.default or 0)
        for column in METRIC_AGGREGATE_COLUMNS
    }


def _apply_increment(current: int, update: MetricUpdate) -> int:
    return current + 1


def _apply_flagged_increment(current: int, update: MetricUpdate) -> int:
    return current + (1 if update.was_flagged else 0)


def _apply_flags_sum(current: int, update: MetricUpdate) -> int:
    return current + int(update.flags_increment or 0)


def _apply_bytes_sum(current: int, update: MetricUpdate) -> int:
    increment = max(int(update.bytes_increment or 0), 0)
    return current + increment


def _apply_duration_sum(current: int, update: MetricUpdate) -> int:
    increment = max(int(update.duration_increment or 0), 0)
    return current + increment


def _apply_assign_duration(_: int, update: MetricUpdate) -> int:
    return max(int(update.duration_increment or 0), 0)


AGGREGATE_UPDATE_HANDLERS: dict[str, Callable[[int, MetricUpdate], int]] = {
    "increment": _apply_increment,
    "flagged_increment": _apply_flagged_increment,
    "flags_sum": _apply_flags_sum,
    "bytes_sum": _apply_bytes_sum,
    "duration_sum": _apply_duration_sum,
    "assign_duration": _apply_assign_duration,
}


@dataclass(slots=True)
class MetricRow:
    aggregates: dict[str, int] = field(default_factory=_default_aggregate_values)
    status_counts: dict[str, int] = field(default_factory=dict)
    last_flagged_at: datetime | None = None
    last_reference: str | None = None
    last_status: str | None = None
    last_details_raw: str | None = None

    def apply_update(self, update: MetricUpdate) -> None:
        for column in METRIC_AGGREGATE_COLUMNS:
            handler_name = column.update_strategy
            if not handler_name:
                continue
            handler = AGGREGATE_UPDATE_HANDLERS.get(handler_name)
            if handler is None:
                raise ValueError(f"Unknown aggregate update strategy: {handler_name}")
            current_value = int(self.aggregates.get(column.name, column.default or 0))
            self.aggregates[column.name] = handler(current_value, update)

        self.last_status = update.status

        status_key = update.status
        current = int(self.status_counts.get(status_key, 0) or 0)
        self.status_counts[status_key] = current + 1

        if update.was_flagged:
            self.last_flagged_at = update.occurred_at
            self.last_reference = update.reference
            self.last_details_raw = update.detail_json
        elif update.store_last_details:
            self.last_details_raw = update.detail_json

    def _value_tuple(self) -> tuple[Any, ...]:
        return (
            *(int(self.aggregates.get(name, 0)) for name in AGGREGATE_COLUMN_NAMES),
            self.last_flagged_at,
            self.last_reference,
            self.last_status,
            encode_json_map(self.status_counts),
            self.last_details_raw,
        )

    def as_update_tuple(self) -> tuple[Any, ...]:
        return self._value_tuple()

    def as_insert_tuple(self) -> tuple[Any, ...]:
        return self._value_tuple()

    def to_public_dict(self) -> dict[str, Any]:
        last_flagged = self.last_flagged_at
        if isinstance(last_flagged, datetime) and last_flagged.tzinfo is None:
            last_flagged = last_flagged.replace(tzinfo=timezone.utc)
        payload = {name: int(self.aggregates.get(name, 0)) for name in AGGREGATE_COLUMN_NAMES}
        payload.update(
            {
                "last_latency_ms": int(self.aggregates.get("last_duration_ms", 0)),
                "status_counts": dict(self.status_counts),
                "last_flagged_at": last_flagged,
                "last_status": self.last_status,
                "last_reference": self.last_reference,
                "last_details": decode_json_map(self.last_details_raw),
                "average_latency_ms": self.average_latency_ms,
            }
        )
        return payload

    @property
    def scans_count(self) -> int:
        return int(self.aggregates.get("scans_count", 0))

    @property
    def total_duration_ms(self) -> int:
        return int(self.aggregates.get("total_duration_ms", 0))

    @property
    def average_latency_ms(self) -> float:
        scans = self.scans_count
        if scans <= 0:
            return 0.0
        return float(self.total_duration_ms) / float(scans)

    @classmethod
    def from_db_row(cls, values: Sequence[Any]) -> MetricRow:
        aggregates: dict[str, int] = {}
        offset = 0
        for column in METRIC_AGGREGATE_COLUMNS:
            raw_value = values[offset] if offset < len(values) else None
            aggregates[column.name] = int(raw_value or 0)
            offset += 1
        last_flagged_at = values[offset] if offset < len(values) else None
        offset += 1
        last_reference = values[offset] if offset < len(values) else None
        offset += 1
        last_status = values[offset] if offset < len(values) else None
        offset += 1
        status_counts_raw = values[offset] if offset < len(values) else None
        offset += 1
        last_details_raw = values[offset] if offset < len(values) else None

        return cls(
            aggregates=aggregates,
            last_flagged_at=last_flagged_at,
            last_reference=last_reference,
            last_status=last_status,
            status_counts=decode_json_map(status_counts_raw),
            last_details_raw=_normalise_json_text(last_details_raw),
        )

    @classmethod
    def empty(cls) -> MetricRow:
        return cls()


def build_metric_update(
    *,
    status: str,
    was_flagged: bool,
    flags_increment: int,
    bytes_increment: int,
    duration_increment: int,
    reference: str | None,
    detail_json: str,
    store_last_details: bool,
    occurred_at: datetime | None,
) -> MetricUpdate:
    return MetricUpdate(
        status=status,
        was_flagged=was_flagged,
        flags_increment=flags_increment,
        bytes_increment=bytes_increment,
        duration_increment=duration_increment,
        reference=reference,
        detail_json=detail_json,
        store_last_details=store_last_details,
        occurred_at=occurred_at,
    )
