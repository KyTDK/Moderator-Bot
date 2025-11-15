from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

from .backend.rollups import summarise_rollups
from .backend.totals import fetch_metric_totals

__all__ = ["LatencyBreakdown", "compute_latency_breakdown"]


class LatencyBreakdown(Dict[str, Any]):
    pass


def _safe_divide(numerator: Optional[float], denominator: Optional[float]) -> Optional[float]:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return numerator / denominator


def _extract_numeric(
    payload: Mapping[str, Any] | None,
    *keys: str,
    coerce,
) -> Optional[float]:
    if payload is None or not isinstance(payload, Mapping):
        return None
    return coerce(_first_present(payload, *keys))


def _extract_int(payload: Mapping[str, Any] | None, *keys: str) -> Optional[int]:
    return _extract_numeric(payload, *keys, coerce=_coerce_int)


def _extract_float(payload: Mapping[str, Any] | None, *keys: str) -> Optional[float]:
    return _extract_numeric(payload, *keys, coerce=_coerce_float)


@dataclass(slots=True)
class LatencyStats:
    label: str
    scans: Optional[int] = None
    total_duration_ms: Optional[float] = None
    average_latency_ms: Optional[float] = None
    frames_scanned: Optional[float] = None
    average_latency_per_frame_ms: Optional[float] = None

    @classmethod
    def from_payload(cls, *, label: str, payload: Mapping[str, Any] | None) -> "LatencyStats":
        scans = _extract_int(payload, "scans_count", "scans")
        total_duration = _extract_float(payload, "total_duration_ms", "duration_total_ms")
        frames = _extract_float(payload, "total_frames_scanned", "frames_total_scanned")
        per_frame = _extract_float(payload, "average_latency_per_frame_ms")
        average_latency = _safe_divide(total_duration, scans)
        if per_frame is None:
            per_frame = _safe_divide(total_duration, frames)
        return cls(
            label=label,
            scans=scans,
            total_duration_ms=total_duration,
            average_latency_ms=average_latency,
            frames_scanned=frames,
            average_latency_per_frame_ms=per_frame,
        )

    def as_dict(self) -> dict[str, Optional[float]]:
        return {
            "label": self.label,
            "scans": self.scans,
            "total_duration_ms": self.total_duration_ms,
            "average_latency_ms": self.average_latency_ms,
            "frames_scanned": self.frames_scanned,
            "average_latency_per_frame_ms": self.average_latency_per_frame_ms,
        }


async def compute_latency_breakdown() -> LatencyBreakdown:
    totals = await fetch_metric_totals()
    summary = await summarise_rollups()

    overall_stats = LatencyStats.from_payload(label="overall", payload=totals)
    overall = overall_stats.as_dict()
    overall["acceleration"] = _extract_acceleration_breakdown(totals.get("acceleration"))

    by_type: dict[str, dict[str, Optional[float]]] = {}
    if isinstance(summary, list):
        for bucket in summary:
            if not isinstance(bucket, Mapping):
                continue
            content_type = bucket.get("content_type") or "unknown"
            stats = LatencyStats.from_payload(label=content_type, payload=bucket)
            payload = stats.as_dict()
            payload["acceleration"] = _extract_acceleration_breakdown(bucket.get("acceleration"))
            by_type[content_type] = payload

    video = by_type.get("video", {})
    image = by_type.get("image", {})

    return LatencyBreakdown(
        overall=overall,
        by_type=by_type,
        video=video,
        image=image,
    )
def _extract_acceleration_breakdown(source: Any) -> Dict[str, Dict[str, Optional[float]]]:
    breakdown: dict[str, dict[str, Optional[float]]] = {}
    if not isinstance(source, dict):
        return breakdown
    for name, payload in source.items():
        stats = LatencyStats.from_payload(label=name, payload=payload if isinstance(payload, Mapping) else None)
        breakdown[name] = stats.as_dict()
    return breakdown


def _coerce_int(value: Any) -> Optional[int]:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_present(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value is not None:
            return value
    return None
