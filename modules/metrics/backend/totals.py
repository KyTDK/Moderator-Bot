from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from ._redis import get_redis_client
from .baselines import apply_count_baselines, fetch_count_baselines
from .keys import totals_key, totals_status_key
from .hydration import MetricSnapshot
from .serialization import coerce_int, ensure_utc, json_dumps, parse_iso_datetime


async def fetch_metric_totals() -> dict[str, Any]:
    client = await get_redis_client()
    totals_hash_key = totals_key()
    totals_hash = await client.hgetall(totals_hash_key)
    baseline_counts = await fetch_count_baselines(client, totals_hash_key)
    apply_count_baselines(totals_hash, baseline_counts)
    status_counts_raw = await client.hgetall(totals_status_key())

    snapshot = MetricSnapshot.from_hash(totals_hash)
    payload = snapshot.to_payload()
    payload["status_counts"] = {name: coerce_int(value) for name, value in status_counts_raw.items()}
    return payload


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
        "total_frames_media": int(aggregates.get("total_frames_media", 0)),
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
