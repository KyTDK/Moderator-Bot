from __future__ import annotations

from datetime import date, datetime
from typing import Any, Mapping

from ._redis import get_redis_client
from .acceleration import (
    ACCELERATION_PREFIXES,
    accumulate_summary_acceleration,
    empty_summary_acceleration,
    finalise_summary_acceleration_bucket,
    hydrate_acceleration_metrics,
)
from .keys import (
    parse_rollup_key,
    rollup_guild_index_key,
    rollup_index_key,
    rollup_key,
    rollup_status_key,
)
from .serialization import (
    coerce_int,
    compute_average,
    compute_stddev,
    compute_frame_metrics,
    ensure_utc,
    json_dumps,
    json_loads,
    normalise_since,
    parse_iso_datetime,
)


async def fetch_metric_rollups(
    *,
    guild_id: int | None = None,
    content_type: str | None = None,
    since: date | datetime | None = None,
    limit: int = 30,
) -> list[dict[str, Any]]:
    return await _fetch_rollups(
        guild_id=guild_id,
        content_type=content_type,
        since=since,
        limit=limit,
        fallback_to_global=True,
    )


async def fetch_global_rollups(
    *,
    content_type: str | None = None,
    since: date | datetime | None = None,
    limit: int = 30,
) -> list[dict[str, Any]]:
    return await _fetch_rollups(
        guild_id=None,
        content_type=content_type,
        since=since,
        limit=limit,
        fallback_to_global=False,
    )


async def _fetch_rollups(
    *,
    guild_id: int | None,
    content_type: str | None,
    since: date | datetime | None,
    limit: int,
    fallback_to_global: bool,
) -> list[dict[str, Any]]:
    client = await get_redis_client()
    since_date = normalise_since(since)
    min_score = float(since_date.toordinal()) if since_date else float("-inf")

    index_key = rollup_guild_index_key(guild_id)
    global_index = rollup_index_key()

    candidates = await _collect_rollup_keys(
        client,
        index_key=index_key,
        global_index=global_index,
        min_score=min_score,
        limit=limit,
        fallback_to_global=fallback_to_global,
    )

    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for key in candidates:
        if key in seen:
            continue
        seen.add(key)
        parsed = parse_rollup_key(key)
        if not parsed:
            continue
        metric_date, guild_value, content = parsed
        if since_date and metric_date < since_date:
            continue
        if guild_id is not None and guild_value != guild_id and not (guild_value is None and guild_id in (0, None)):
            continue
        if content_type and content != content_type:
            continue

        rollup_data = await client.hgetall(key)
        status_counts_raw = await client.hgetall(rollup_status_key(key))
        rollup = _hydrate_rollup(metric_date, guild_value, content, rollup_data, status_counts_raw)
        results.append(rollup)
        if len(results) >= limit:
            break
    return results


async def summarise_rollups(
    *,
    guild_id: int | None = None,
    since: date | datetime | None = None,
) -> list[dict[str, Any]]:
    client = await get_redis_client()
    since_date = normalise_since(since)
    min_score = float(since_date.toordinal()) if since_date else float("-inf")

    index_key = rollup_guild_index_key(guild_id)
    global_index = rollup_index_key()

    candidates: list[str] = []
    for key in (index_key, global_index):
        if key == index_key or not candidates:
            fetched = await client.zrevrangebyscore(key, "+inf", min_score)
            candidates.extend(fetched)
        if candidates:
            break

    summary: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    for key in candidates:
        if key in seen:
            continue
        seen.add(key)
        parsed = parse_rollup_key(key)
        if not parsed:
            continue
        metric_date, guild_value, content = parsed
        if since_date and metric_date < since_date:
            continue
        if guild_id is not None and guild_value != guild_id and not (guild_value is None and guild_id in (0, None)):
            continue

        rollup_data = await client.hgetall(key)
        bucket = summary.setdefault(content, _empty_summary_bucket(content))
        _accumulate_summary_from_rollup(bucket, rollup_data)

    for bucket in summary.values():
        _finalise_summary_bucket(bucket)

    ordered = sorted(summary.values(), key=lambda payload: payload["scans"], reverse=True)
    return ordered


async def import_rollup_snapshot(
    *,
    metric_date: date,
    guild_id: int | None,
    content_type: str,
    aggregates: Mapping[str, int],
    status_counts: Mapping[str, int],
    last_flagged_at: datetime | None,
    last_status: str | None,
    last_reference: str | None,
    last_details: Any | None,
) -> None:
    client = await get_redis_client()
    rollup_key_value = rollup_key(metric_date, guild_id, content_type)
    status_key = rollup_status_key(rollup_key_value)

    await client.delete(rollup_key_value)
    await client.delete(status_key)

    mapping = {
        "scans_count": int(aggregates.get("scans_count", 0)),
        "flagged_count": int(aggregates.get("flagged_count", 0)),
        "flags_sum": int(aggregates.get("flags_sum", 0)),
        "total_bytes": int(aggregates.get("total_bytes", 0)),
        "total_duration_ms": int(aggregates.get("total_duration_ms", 0)),
        "last_duration_ms": int(aggregates.get("last_duration_ms", 0)),
    }
    if last_flagged_at:
        mapping["last_flagged_at"] = ensure_utc(last_flagged_at).isoformat()
    if last_status is not None:
        mapping["last_status"] = last_status
    if last_reference:
        mapping["last_reference"] = last_reference
    if last_details is not None:
        mapping["last_details"] = json_dumps(last_details)

    await client.hset(rollup_key_value, mapping=mapping)
    if status_counts:
        await client.hset(
            status_key,
            mapping={name: int(value) for name, value in status_counts.items()},
        )

    score = float(metric_date.toordinal())
    await client.zadd(rollup_index_key(), {rollup_key_value: score})
    await client.zadd(rollup_guild_index_key(guild_id), {rollup_key_value: score})


def _hydrate_rollup(
    metric_date: date,
    guild_id: int | None,
    content_type: str,
    rollup_data: Mapping[str, str],
    status_counts_raw: Mapping[str, str],
) -> dict[str, Any]:
    scans_count = coerce_int(rollup_data.get("scans_count"))
    flagged_count = coerce_int(rollup_data.get("flagged_count"))
    flags_sum = coerce_int(rollup_data.get("flags_sum"))
    total_bytes = coerce_int(rollup_data.get("total_bytes"))
    total_duration = coerce_int(rollup_data.get("total_duration_ms"))
    total_bytes_sq = coerce_int(rollup_data.get("total_bytes_sq"))
    total_duration_sq = coerce_int(rollup_data.get("total_duration_sq_ms"))
    total_frames_scanned = coerce_int(rollup_data.get("total_frames_scanned"))
    total_frames_target = coerce_int(rollup_data.get("total_frames_target"))
    last_duration = coerce_int(rollup_data.get("last_duration_ms"))
    last_status = rollup_data.get("last_status")
    last_reference_raw = rollup_data.get("last_reference")
    last_reference = last_reference_raw if last_reference_raw else None
    last_flagged_at = parse_iso_datetime(rollup_data.get("last_flagged_at"))
    last_details = json_loads(rollup_data.get("last_details"))
    updated_at = parse_iso_datetime(rollup_data.get("updated_at"))

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
        result_key: hydrate_acceleration_metrics(prefix, rollup_data)
        for result_key, prefix in ACCELERATION_PREFIXES.items()
    }

    return {
        "metric_date": metric_date,
        "guild_id": guild_id,
        "content_type": content_type,
        "scans_count": scans_count,
        "flagged_count": flagged_count,
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
        "status_counts": status_counts,
        "last_flagged_at": last_flagged_at,
        "last_status": last_status,
        "last_reference": last_reference,
        "last_details": last_details,
        "updated_at": updated_at,
        "acceleration": acceleration_breakdown,
    }


def _empty_summary_bucket(content: str) -> dict[str, Any]:
    return {
        "content_type": content,
        "scans": 0,
        "flagged": 0,
        "flags_sum": 0,
        "bytes_total": 0,
        "bytes_total_sq": 0,
        "duration_total_ms": 0,
        "duration_total_sq_ms": 0,
        "frames_total_scanned": 0,
        "frames_total_target": 0,
        "acceleration": empty_summary_acceleration(),
    }


def _accumulate_summary_from_rollup(bucket: dict[str, Any], rollup_data: Mapping[str, str]) -> None:
    bucket["scans"] += coerce_int(rollup_data.get("scans_count"))
    bucket["flagged"] += coerce_int(rollup_data.get("flagged_count"))
    bucket["flags_sum"] += coerce_int(rollup_data.get("flags_sum"))
    bucket["bytes_total"] += coerce_int(rollup_data.get("total_bytes"))
    bucket["bytes_total_sq"] += coerce_int(rollup_data.get("total_bytes_sq"))
    bucket["duration_total_ms"] += coerce_int(rollup_data.get("total_duration_ms"))
    bucket["duration_total_sq_ms"] += coerce_int(rollup_data.get("total_duration_sq_ms"))
    bucket["frames_total_scanned"] += coerce_int(rollup_data.get("total_frames_scanned"))
    bucket["frames_total_target"] += coerce_int(rollup_data.get("total_frames_target"))

    accumulate_summary_acceleration(bucket["acceleration"], rollup_data)


def _finalise_summary_bucket(bucket: dict[str, Any]) -> None:
    scans = bucket["scans"]
    bucket["average_latency_ms"] = compute_average(bucket["duration_total_ms"], scans)
    bucket["latency_std_dev_ms"] = compute_stddev(bucket["duration_total_ms"], bucket["duration_total_sq_ms"], scans)
    bucket["average_bytes"] = compute_average(bucket["bytes_total"], scans)
    bucket["bytes_std_dev"] = compute_stddev(bucket["bytes_total"], bucket["bytes_total_sq"], scans)
    bucket["flagged_rate"] = compute_average(bucket["flagged"], scans)
    bucket["average_flags_per_scan"] = compute_average(bucket["flags_sum"], scans)
    (
        bucket["average_frames_per_scan"],
        bucket["average_latency_per_frame_ms"],
        bucket["frames_per_second"],
        bucket["frame_coverage_rate"],
    ) = compute_frame_metrics(
        total_duration_ms=bucket["duration_total_ms"],
        total_frames_scanned=bucket["frames_total_scanned"],
        total_frames_target=bucket["frames_total_target"],
        scan_count=scans,
    )

    for accel_bucket in bucket["acceleration"].values():
        finalise_summary_acceleration_bucket(accel_bucket)


__all__ = [
    "fetch_metric_rollups",
    "fetch_global_rollups",
    "import_rollup_snapshot",
    "summarise_rollups",
]


async def _collect_rollup_keys(
    client: Any,
    *,
    index_key: str,
    global_index: str,
    min_score: float,
    limit: int,
    fallback_to_global: bool,
) -> list[str]:
    keys_to_try: list[str] = [index_key]
    if fallback_to_global and global_index not in keys_to_try:
        keys_to_try.append(global_index)

    candidates: list[str] = []
    fetch_size = max(limit * 5, 50)
    for key in keys_to_try:
        fetched = await client.zrevrangebyscore(
            key,
            "+inf",
            min_score,
            start=0,
            num=fetch_size,
        )
        candidates.extend(fetched)
        if candidates:
            break
    return candidates
