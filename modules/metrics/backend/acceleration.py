from __future__ import annotations

from typing import Any, Dict, Iterable, Mapping, Tuple

from .serialization import (
    coerce_int,
    compute_average,
    compute_stddev,
    json_loads,
    parse_iso_datetime,
)

ACCELERATION_PREFIXES: Dict[str, str] = {
    "accelerated": "accelerated",
    "non_accelerated": "non_accelerated",
    "unknown": "unknown_acceleration",
}


def resolve_acceleration_prefix(accelerated: bool | None) -> str:
    if accelerated is True:
        return "accelerated"
    if accelerated is False:
        return "non_accelerated"
    return "unknown_acceleration"


def iter_acceleration_prefixes() -> Iterable[Tuple[str, str]]:
    return ACCELERATION_PREFIXES.items()


def hydrate_acceleration_metrics(prefix: str, source: Mapping[str, str]) -> dict[str, Any]:
    scans = coerce_int(source.get(f"{prefix}_scans_count"))
    flagged = coerce_int(source.get(f"{prefix}_flagged_count"))
    flags_sum = coerce_int(source.get(f"{prefix}_flags_sum"))
    total_bytes = coerce_int(source.get(f"{prefix}_total_bytes"))
    total_bytes_sq = coerce_int(source.get(f"{prefix}_total_bytes_sq"))
    total_duration = coerce_int(source.get(f"{prefix}_total_duration_ms"))
    total_duration_sq = coerce_int(source.get(f"{prefix}_total_duration_sq_ms"))
    last_duration = coerce_int(source.get(f"{prefix}_last_duration_ms"))
    total_frames_scanned = coerce_int(source.get(f"{prefix}_total_frames_scanned"))
    total_frames_target = coerce_int(source.get(f"{prefix}_total_frames_target"))
    last_status = source.get(f"{prefix}_last_status")
    last_reference_raw = source.get(f"{prefix}_last_reference")
    last_reference = last_reference_raw if last_reference_raw else None
    last_flagged_at = parse_iso_datetime(source.get(f"{prefix}_last_flagged_at"))
    last_at = parse_iso_datetime(source.get(f"{prefix}_last_at"))
    last_details = json_loads(source.get(f"{prefix}_last_details"))

    average_latency = compute_average(total_duration, scans)
    latency_std_dev = compute_stddev(total_duration, total_duration_sq, scans)
    average_bytes = compute_average(total_bytes, scans)
    bytes_std_dev = compute_stddev(total_bytes, total_bytes_sq, scans)
    flagged_rate = compute_average(flagged, scans)
    average_flags = compute_average(flags_sum, scans)
    average_frames_per_scan = compute_average(total_frames_scanned, scans)
    frame_denominator = total_frames_scanned if total_frames_scanned > 0 else scans
    average_latency_per_frame = compute_average(total_duration, frame_denominator)
    frames_per_second = (float(total_frames_scanned) / float(total_duration) * 1000.0) if total_duration > 0 else 0.0
    frame_coverage_rate = compute_average(total_frames_scanned, total_frames_target)

    return {
        "scans_count": scans,
        "flagged_count": flagged,
        "flags_sum": flags_sum,
        "flagged_rate": flagged_rate,
        "average_flags_per_scan": average_flags,
        "total_bytes": total_bytes,
        "total_bytes_sq": total_bytes_sq,
        "average_bytes": average_bytes,
        "bytes_std_dev": bytes_std_dev,
        "total_duration_ms": total_duration,
        "total_duration_sq_ms": total_duration_sq,
        "total_frames_scanned": total_frames_scanned,
        "total_frames_target": total_frames_target,
        "average_frames_per_scan": average_frames_per_scan,
        "last_latency_ms": last_duration,
        "average_latency_ms": average_latency,
        "latency_std_dev_ms": latency_std_dev,
        "average_latency_per_frame_ms": average_latency_per_frame,
        "frames_per_second": frames_per_second,
        "frame_coverage_rate": frame_coverage_rate,
        "last_status": last_status,
        "last_reference": last_reference,
        "last_flagged_at": last_flagged_at,
        "last_at": last_at,
        "last_details": last_details,
    }


def empty_summary_acceleration() -> dict[str, dict[str, Any]]:
    return {result_key: _empty_acceleration_totals() for result_key, _ in iter_acceleration_prefixes()}


def accumulate_summary_acceleration(summary_acceleration: dict[str, dict[str, Any]], source: Mapping[str, str]) -> None:
    for result_key, prefix in iter_acceleration_prefixes():
        bucket = summary_acceleration[result_key]
        bucket["scans"] += coerce_int(source.get(f"{prefix}_scans_count"))
        bucket["flagged"] += coerce_int(source.get(f"{prefix}_flagged_count"))
        bucket["flags_sum"] += coerce_int(source.get(f"{prefix}_flags_sum"))
        bucket["bytes_total"] += coerce_int(source.get(f"{prefix}_total_bytes"))
        bucket["bytes_total_sq"] += coerce_int(source.get(f"{prefix}_total_bytes_sq"))
        bucket["duration_total_ms"] += coerce_int(source.get(f"{prefix}_total_duration_ms"))
        bucket["duration_total_sq_ms"] += coerce_int(source.get(f"{prefix}_total_duration_sq_ms"))
        bucket["frames_total_scanned"] += coerce_int(source.get(f"{prefix}_total_frames_scanned"))
        bucket["frames_total_target"] += coerce_int(source.get(f"{prefix}_total_frames_target"))


def finalise_summary_acceleration_bucket(accel_bucket: dict[str, Any]) -> None:
    scans = accel_bucket["scans"]
    accel_bucket["average_latency_ms"] = compute_average(accel_bucket["duration_total_ms"], scans)
    accel_bucket["latency_std_dev_ms"] = compute_stddev(
        accel_bucket["duration_total_ms"],
        accel_bucket["duration_total_sq_ms"],
        scans,
    )
    accel_bucket["average_bytes"] = compute_average(accel_bucket["bytes_total"], scans)
    accel_bucket["bytes_std_dev"] = compute_stddev(accel_bucket["bytes_total"], accel_bucket["bytes_total_sq"], scans)
    accel_bucket["flagged_rate"] = compute_average(accel_bucket["flagged"], scans)
    accel_bucket["average_flags_per_scan"] = compute_average(accel_bucket["flags_sum"], scans)
    accel_bucket["average_frames_per_scan"] = compute_average(accel_bucket["frames_total_scanned"], scans)
    frame_denominator = accel_bucket["frames_total_scanned"] if accel_bucket["frames_total_scanned"] > 0 else scans
    accel_bucket["average_latency_per_frame_ms"] = compute_average(accel_bucket["duration_total_ms"], frame_denominator)
    accel_bucket["frames_per_second"] = (
        float(accel_bucket["frames_total_scanned"]) / float(accel_bucket["duration_total_ms"]) * 1000.0
        if accel_bucket["duration_total_ms"] > 0
        else 0.0
    )
    accel_bucket["frame_coverage_rate"] = compute_average(
        accel_bucket["frames_total_scanned"],
        accel_bucket["frames_total_target"],
    )


def _empty_acceleration_totals() -> dict[str, Any]:
    return {
        "scans": 0,
        "flagged": 0,
        "flags_sum": 0,
        "bytes_total": 0,
        "bytes_total_sq": 0,
        "duration_total_ms": 0,
        "duration_total_sq_ms": 0,
        "frames_total_scanned": 0,
        "frames_total_target": 0,
    }


__all__ = [
    "ACCELERATION_PREFIXES",
    "accumulate_summary_acceleration",
    "empty_summary_acceleration",
    "finalise_summary_acceleration_bucket",
    "hydrate_acceleration_metrics",
    "iter_acceleration_prefixes",
    "resolve_acceleration_prefix",
]
