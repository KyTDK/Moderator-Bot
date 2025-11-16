from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from .acceleration import ACCELERATION_PREFIXES, hydrate_acceleration_metrics
from .serialization import (
    coerce_int,
    compute_average,
    compute_frame_metrics,
    compute_stddev,
    json_loads,
    parse_iso_datetime,
)

__all__ = ["MetricSnapshot"]


@dataclass(slots=True)
class MetricSnapshot:
    """Normalised aggregate metrics computed from a Redis hash."""

    scans_count: int
    flagged_count: int
    flags_sum: int
    total_bytes: int
    total_bytes_sq: int
    total_duration_ms: int
    total_duration_sq_ms: int
    total_frames_scanned: int
    total_frames_target: int
    total_frames_media: int
    last_duration_ms: int
    last_status: str | None
    last_reference: str | None
    last_flagged_at: datetime | None
    last_details: Any
    updated_at: datetime | None
    acceleration: dict[str, Any]

    @classmethod
    def from_hash(cls, payload: Mapping[str, Any]) -> "MetricSnapshot":
        scans_count = coerce_int(payload.get("scans_count"))
        flagged_count = coerce_int(payload.get("flagged_count"))
        flags_sum = coerce_int(payload.get("flags_sum"))
        total_bytes = coerce_int(payload.get("total_bytes"))
        total_bytes_sq = coerce_int(payload.get("total_bytes_sq"))
        total_duration_ms = coerce_int(payload.get("total_duration_ms"))
        total_duration_sq_ms = coerce_int(payload.get("total_duration_sq_ms"))
        total_frames_scanned = coerce_int(payload.get("total_frames_scanned"))
        total_frames_target = coerce_int(payload.get("total_frames_target"))
        total_frames_media = coerce_int(payload.get("total_frames_media"))
        last_duration_ms = coerce_int(payload.get("last_duration_ms"))
        last_status = payload.get("last_status")
        last_reference_raw = payload.get("last_reference")
        last_reference = last_reference_raw if last_reference_raw else None
        last_flagged_at = parse_iso_datetime(payload.get("last_flagged_at"))
        last_details = json_loads(payload.get("last_details"))
        updated_at = parse_iso_datetime(payload.get("updated_at"))
        acceleration = {
            result_key: hydrate_acceleration_metrics(prefix, payload)
            for result_key, prefix in ACCELERATION_PREFIXES.items()
        }
        return cls(
            scans_count=scans_count,
            flagged_count=flagged_count,
            flags_sum=flags_sum,
            total_bytes=total_bytes,
            total_bytes_sq=total_bytes_sq,
            total_duration_ms=total_duration_ms,
            total_duration_sq_ms=total_duration_sq_ms,
            total_frames_scanned=total_frames_scanned,
            total_frames_target=total_frames_target,
            total_frames_media=total_frames_media,
            last_duration_ms=last_duration_ms,
            last_status=last_status,
            last_reference=last_reference,
            last_flagged_at=last_flagged_at,
            last_details=last_details,
            updated_at=updated_at,
            acceleration=acceleration,
        )

    def to_payload(self) -> dict[str, Any]:
        average_latency = compute_average(self.total_duration_ms, self.scans_count)
        latency_std_dev = compute_stddev(self.total_duration_ms, self.total_duration_sq_ms, self.scans_count)
        average_bytes = compute_average(self.total_bytes, self.scans_count)
        bytes_std_dev = compute_stddev(self.total_bytes, self.total_bytes_sq, self.scans_count)
        flagged_rate = compute_average(self.flagged_count, self.scans_count)
        average_flags = compute_average(self.flags_sum, self.scans_count)
        (
            average_frames_per_scan,
            average_latency_per_frame_ms,
            frames_per_second,
            frame_coverage_rate,
        ) = compute_frame_metrics(
            total_duration_ms=self.total_duration_ms,
            total_frames_scanned=self.total_frames_scanned,
            total_frames_target=self.total_frames_target,
            total_frames_media=self.total_frames_media,
            scan_count=self.scans_count,
        )

        return {
            "scans_count": self.scans_count,
            "flagged_count": self.flagged_count,
            "flags_sum": self.flags_sum,
            "total_bytes": self.total_bytes,
            "total_bytes_sq": self.total_bytes_sq,
            "average_bytes": average_bytes,
            "bytes_std_dev": bytes_std_dev,
            "total_duration_ms": self.total_duration_ms,
            "total_duration_sq_ms": self.total_duration_sq_ms,
            "total_frames_scanned": self.total_frames_scanned,
            "total_frames_target": self.total_frames_target,
            "total_frames_media": self.total_frames_media,
            "average_frames_per_scan": average_frames_per_scan,
            "last_latency_ms": self.last_duration_ms,
            "average_latency_ms": average_latency,
            "latency_std_dev_ms": latency_std_dev,
            "average_latency_per_frame_ms": average_latency_per_frame_ms,
            "frames_per_second": frames_per_second,
            "frame_coverage_rate": frame_coverage_rate,
            "flagged_rate": flagged_rate,
            "average_flags_per_scan": average_flags,
            "last_flagged_at": self.last_flagged_at,
            "last_status": self.last_status,
            "last_reference": self.last_reference,
            "last_details": self.last_details,
            "updated_at": self.updated_at,
            "acceleration": self.acceleration,
        }
