from __future__ import annotations

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


async def compute_latency_breakdown() -> LatencyBreakdown:
    totals = await fetch_metric_totals()
    summary = await summarise_rollups()

    overall = _extract_latency_stats(
        label="overall",
        scans=totals.get("scans_count"),
        total_duration_ms=totals.get("total_duration_ms"),
        total_frames=totals.get("total_frames_scanned"),
        per_frame_ms=totals.get("average_latency_per_frame_ms"),
    )
    overall["acceleration"] = _extract_acceleration_breakdown(totals.get("acceleration"))

    by_type = {}
    for bucket in summary:
        content_type = bucket.get("content_type") or "unknown"
        by_type[content_type] = _extract_latency_stats(
            label=content_type,
            scans=bucket.get("scans"),
            total_duration_ms=bucket.get("total_duration_ms"),
            total_frames=bucket.get("total_frames_scanned"),
            per_frame_ms=bucket.get("average_latency_per_frame_ms"),
        )
        by_type[content_type]["acceleration"] = _extract_acceleration_breakdown(bucket.get("acceleration"))

    video = by_type.get("video", {})
    image = by_type.get("image", {})

    return LatencyBreakdown(
        overall=overall,
        by_type=by_type,
        video=video,
        image=image,
    )


def _extract_latency_stats(
    *,
    label: str,
    scans: Any,
    total_duration_ms: Any,
    total_frames: Any,
    per_frame_ms: Any,
) -> Dict[str, Optional[float]]:
    scans_count = _coerce_int(scans)
    total_duration = _coerce_float(total_duration_ms)
    frames = _coerce_float(total_frames)
    avg_latency = _safe_divide(total_duration, scans_count)
    avg_latency_per_frame = _coerce_float(per_frame_ms) or _safe_divide(total_duration, frames)

    return {
        "label": label,
        "scans": scans_count,
        "total_duration_ms": total_duration,
        "average_latency_ms": avg_latency,
        "frames_scanned": frames,
        "average_latency_per_frame_ms": avg_latency_per_frame,
    }


def _extract_acceleration_breakdown(source: Any) -> Dict[str, Dict[str, Optional[float]]]:
    breakdown: dict[str, dict[str, Optional[float]]] = {}
    if not isinstance(source, dict):
        return breakdown
    for name, payload in source.items():
        if not isinstance(payload, dict):
            continue
        breakdown[name] = _extract_latency_stats(
            label=name,
            scans=_coerce_int(_first_present(payload, "scans_count", "scans")),
            total_duration_ms=_coerce_float(_first_present(payload, "total_duration_ms", "duration_total_ms")),
            total_frames=_coerce_float(_first_present(payload, "total_frames_scanned", "frames_total_scanned")),
            per_frame_ms=_coerce_float(payload.get("average_latency_per_frame_ms")),
        )
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
