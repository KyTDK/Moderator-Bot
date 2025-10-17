from __future__ import annotations

from datetime import date, datetime
from typing import Any, Mapping

from ._redis import get_redis_client
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
    client = await get_redis_client()
    since_date = normalise_since(since)
    min_score = float(since_date.toordinal()) if since_date else float("-inf")

    index_key = rollup_guild_index_key(guild_id)
    global_index = rollup_index_key()

    candidates: list[str] = []
    for key in (index_key, global_index):
        if key == index_key or not candidates:
            fetched = await client.zrevrangebyscore(
                key,
                "+inf",
                min_score,
                start=0,
                num=max(limit * 5, 50),
            )
            candidates.extend(fetched)
        if candidates:
            break

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
        scans_count = coerce_int(rollup_data.get("scans_count"))
        flagged_count = coerce_int(rollup_data.get("flagged_count"))
        flags_sum = coerce_int(rollup_data.get("flags_sum"))
        total_bytes = coerce_int(rollup_data.get("total_bytes"))
        total_duration = coerce_int(rollup_data.get("total_duration_ms"))

        bucket = summary.setdefault(
            content,
            {
                "content_type": content,
                "scans": 0,
                "flagged": 0,
                "flags_sum": 0,
                "bytes_total": 0,
                "duration_total_ms": 0,
            },
        )
        bucket["scans"] += scans_count
        bucket["flagged"] += flagged_count
        bucket["flags_sum"] += flags_sum
        bucket["bytes_total"] += total_bytes
        bucket["duration_total_ms"] += total_duration

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
    last_duration = coerce_int(rollup_data.get("last_duration_ms"))
    last_status = rollup_data.get("last_status")
    last_reference_raw = rollup_data.get("last_reference")
    last_reference = last_reference_raw if last_reference_raw else None
    last_flagged_at = parse_iso_datetime(rollup_data.get("last_flagged_at"))
    last_details = json_loads(rollup_data.get("last_details"))

    status_counts = {name: coerce_int(value) for name, value in status_counts_raw.items()}
    average_latency = compute_average(total_duration, scans_count)

    return {
        "metric_date": metric_date,
        "guild_id": guild_id,
        "content_type": content_type,
        "scans_count": scans_count,
        "flagged_count": flagged_count,
        "flags_sum": flags_sum,
        "total_bytes": total_bytes,
        "total_duration_ms": total_duration,
        "last_latency_ms": last_duration,
        "average_latency_ms": average_latency,
        "status_counts": status_counts,
        "last_flagged_at": last_flagged_at,
        "last_status": last_status,
        "last_reference": last_reference,
        "last_details": last_details,
    }


__all__ = [
    "fetch_metric_rollups",
    "import_rollup_snapshot",
    "summarise_rollups",
]
