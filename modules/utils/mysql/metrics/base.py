from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Sequence


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


@dataclass(slots=True)
class MetricRow:
    scans_count: int = 0
    flagged_count: int = 0
    flags_sum: int = 0
    total_bytes: int = 0
    total_duration_ms: int = 0
    last_duration_ms: int = 0
    status_counts: dict[str, int] = field(default_factory=dict)
    last_flagged_at: datetime | None = None
    last_reference: str | None = None
    last_status: str | None = None
    last_details_raw: str | None = None

    def apply_update(self, update: MetricUpdate) -> None:
        self.scans_count += 1
        if update.was_flagged:
            self.flagged_count += 1
        self.flags_sum += int(update.flags_increment or 0)
        self.total_bytes += int(update.bytes_increment or 0)
        self.total_duration_ms += int(update.duration_increment or 0)
        self.last_duration_ms = int(update.duration_increment or 0)
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

    def as_update_tuple(self) -> tuple[Any, ...]:
        return (
            self.scans_count,
            self.flagged_count,
            self.flags_sum,
            self.total_bytes,
            self.total_duration_ms,
            self.last_duration_ms,
            self.last_flagged_at,
            self.last_reference,
            self.last_status,
            encode_json_map(self.status_counts),
            self.last_details_raw,
        )

    def as_insert_tuple(self) -> tuple[Any, ...]:
        return (
            self.scans_count,
            self.flagged_count,
            self.flags_sum,
            self.total_bytes,
            self.total_duration_ms,
            self.last_duration_ms,
            self.last_flagged_at,
            self.last_reference,
            self.last_status,
            encode_json_map(self.status_counts),
            self.last_details_raw,
        )

    def to_public_dict(self) -> dict[str, Any]:
        last_flagged = self.last_flagged_at
        if isinstance(last_flagged, datetime) and last_flagged.tzinfo is None:
            last_flagged = last_flagged.replace(tzinfo=timezone.utc)
        return {
            "scans_count": int(self.scans_count),
            "flagged_count": int(self.flagged_count),
            "flags_sum": int(self.flags_sum),
            "total_bytes": int(self.total_bytes),
            "total_duration_ms": int(self.total_duration_ms),
            "last_latency_ms": int(self.last_duration_ms),
            "status_counts": dict(self.status_counts),
            "last_flagged_at": last_flagged,
            "last_status": self.last_status,
            "last_reference": self.last_reference,
            "last_details": decode_json_map(self.last_details_raw),
            "average_latency_ms": self.average_latency_ms,
        }

    @property
    def average_latency_ms(self) -> float:
        if self.scans_count <= 0:
            return 0.0
        return float(self.total_duration_ms) / float(self.scans_count)

    @classmethod
    def from_db_row(cls, values: Sequence[Any]) -> MetricRow:
        (
            scans_count,
            flagged_count,
            flags_sum,
            total_bytes,
            total_duration_ms,
            last_duration_ms,
            last_flagged_at,
            last_reference,
            last_status,
            status_counts_raw,
            last_details_raw,
        ) = values
        return cls(
            scans_count=int(scans_count or 0),
            flagged_count=int(flagged_count or 0),
            flags_sum=int(flags_sum or 0),
            total_bytes=int(total_bytes or 0),
            total_duration_ms=int(total_duration_ms or 0),
            last_duration_ms=int(last_duration_ms or 0),
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
