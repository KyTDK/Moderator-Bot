from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from ._redis import get_redis_client
from .acceleration import ACCELERATION_PREFIXES, hydrate_acceleration_metrics
from .keys import totals_key, totals_status_key
from .serialization import (
    coerce_int,
    compute_average,
    compute_frame_metrics,
    compute_stddev,
    ensure_utc,
    json_dumps,
    json_loads,
    parse_iso_datetime,
)


async def fetch_metric_totals() -> dict[str, Any]:
    client = await get_redis_client()
    totals_hash = await client.hgetall(totals_key())
    status_counts_raw = await client.hgetall(totals_status_key())

    scans_count = coerce_int(totals_hash.get("scans_count"))
    flagged_count = coerce_int(totals_hash.get("flagged_count"))
    flags_sum = coerce_int(totals_hash.get("flags_sum"))
    total_bytes = coerce_int(totals_hash.get("total_bytes"))
    total_duration = coerce_int(totals_hash.get("total_duration_ms"))
    total_bytes_sq = coerce_int(totals_hash.get("total_bytes_sq"))
    total_duration_sq = coerce_int(totals_hash.get("total_duration_sq_ms"))
    total_frames_scanned = coerce_int(totals_hash.get("total_frames_scanned"))
    total_frames_target = coerce_int(totals_hash.get("total_frames_target"))
    last_duration = coerce_int(totals_hash.get("last_duration_ms"))
    last_status = totals_hash.get("last_status")
    last_reference_raw = totals_hash.get("last_reference")
    last_reference = last_reference_raw if last_reference_raw else None
    last_flagged_at = parse_iso_datetime(totals_hash.get("last_flagged_at"))
    last_details = json_loads(totals_hash.get("last_details"))
    updated_at = parse_iso_datetime(totals_hash.get("updated_at"))

    status_counts = {name: coerce_int(value) for name, value in status_counts_raw.items()}
    average_latency = compute_average(total_duration, scans_count)
    latency_std_dev = compute_stddev(total_duration, total_duration_sq, scans_count)
    average_bytes = compute_average(total_bytes, scans_count)
    bytes_std_dev = compute_stddev(total_bytes, total_bytes_sq, scans_count)
    flagged_rate = compute_average(flagged_count, scans_count)
    average_flags = compute_average(flags_sum, scans_count)
    average_frames_per_scan, average_latency_per_frame, frames_per_second, frame_coverage_rate = compute_frame_metrics(
        total_duration_ms=total_duration,
        total_frames_scanned=total_frames_scanned,
        total_frames_target=total_frames_target,
        scan_count=scans_count,
    )
    acceleration_breakdown = {
        result_key: hydrate_acceleration_metrics(prefix, totals_hash)
        for result_key, prefix in ACCELERATION_PREFIXES.items()
    }

    return {
        "scans_count": scans_count,
        "flagged_count": flagged_count,
        "flags_sum": flags_sum,
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
        "flagged_rate": flagged_rate,
        "average_flags_per_scan": average_flags,
        "status_counts": status_counts,
        "last_flagged_at": last_flagged_at,
        "last_status": last_status,
        "last_reference": last_reference,
        "last_details": last_details,
        "updated_at": updated_at,
        "acceleration": acceleration_breakdown,
    }


async def import_totals_snapshot(
    *,
    aggregates: Mapping[str, int],
    status_counts: Mapping[str, int],
    last_flagged_at: datetime | None,
    last_status: str | None,
    last_reference: str | None,
    last_details: Any | None,
    updated_at: datetime | None = None,
) -> None:
    client = await get_redis_client()
    totals_hash = totals_key()
    status_hash = totals_status_key()

    await client.delete(totals_hash)
    await client.delete(status_hash)

    mapping = {
        "scans_count": int(aggregates.get("scans_count", 0)),
        "flagged_count": int(aggregates.get("flagged_count", 0)),
        "flags_sum": int(aggregates.get("flags_sum", 0)),
        "total_bytes": int(aggregates.get("total_bytes", 0)),
        "total_duration_ms": int(aggregates.get("total_duration_ms", 0)),
        "last_duration_ms": int(aggregates.get("last_duration_ms", 0)),
        "total_frames_scanned": int(aggregates.get("total_frames_scanned", 0)),
        "total_frames_target": int(aggregates.get("total_frames_target", 0)),
    }
    if last_flagged_at:
        mapping["last_flagged_at"] = ensure_utc(last_flagged_at).isoformat()
    if last_status is not None:
        mapping["last_status"] = last_status
    if last_reference:
        mapping["last_reference"] = last_reference
    if last_details is not None:
        mapping["last_details"] = json_dumps(last_details)

    timestamp = ensure_utc(updated_at).isoformat() if updated_at else datetime.now(timezone.utc).isoformat()
    mapping["updated_at"] = timestamp

    await client.hset(totals_hash, mapping=mapping)
    if status_counts:
        await client.hset(
            status_hash,
            mapping={name: int(value) for name, value in status_counts.items()},
        )


__all__ = ["fetch_metric_totals", "import_totals_snapshot"]
