from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from typing import Any

from ..config import get_metrics_redis_config
from ._redis import RedisError, get_redis_client
from .acceleration import resolve_acceleration_prefix
from .keys import (
    rollup_guild_index_key,
    rollup_index_key,
    rollup_key,
    rollup_status_key,
    totals_key,
    totals_status_key,
)
from .serialization import ensure_utc, json_dumps

_logger = logging.getLogger(__name__)


async def accumulate_media_metric(
    *,
    occurred_at: datetime | None,
    guild_id: int | None,
    content_type: str,
    status: str,
    was_flagged: bool,
    flags_count: int,
    file_size: int | None,
    scan_duration_ms: int | None,
    reference: str | None,
    details: dict[str, Any] | None,
    store_last_details: bool,
    accelerated: bool | None,
) -> None:
    client = await get_redis_client()
    config = get_metrics_redis_config()

    occurred = ensure_utc(occurred_at)
    occurred_iso = occurred.isoformat()
    metric_date = occurred.date()
    rollup_key_value = rollup_key(metric_date, guild_id, content_type)
    status_key = rollup_status_key(rollup_key_value)
    global_rollup_key_value = rollup_key(metric_date, None, content_type)
    global_status_key = rollup_status_key(global_rollup_key_value)
    acceleration_prefix = resolve_acceleration_prefix(accelerated)

    detail_payload: dict[str, Any] = {}
    if details:
        detail_payload.update(details)
    detail_payload.setdefault("status", status)
    detail_payload.setdefault("was_flagged", was_flagged)
    if reference is not None:
        detail_payload.setdefault("reference", reference)
    detail_payload.setdefault("occurred_at", occurred.isoformat())
    detail_json = json_dumps(detail_payload)

    event_payload = {
        "occurred_at": occurred_iso,
        "metric_date": metric_date.isoformat(),
        "guild_id": None if guild_id in (None, 0) else int(guild_id),
        "content_type": content_type,
        "status": status,
        "was_flagged": was_flagged,
        "flags_count": int(flags_count or 0),
        "file_size": file_size,
        "scan_duration_ms": scan_duration_ms,
        "reference": reference,
        "details": detail_payload,
        "accelerated": accelerated,
    }

    stream_kwargs: dict[str, Any] = {}
    if config.stream_maxlen is not None:
        stream_kwargs["maxlen"] = config.stream_maxlen
        stream_kwargs["approximate"] = config.stream_approximate
    try:
        await client.xadd(
            config.stream_name,
            {"event": json.dumps(event_payload, ensure_ascii=False)},
            **stream_kwargs,
        )
    except RedisError as exc:  # pragma: no cover - transient Redis failures
        _logger.warning("Unable to append metrics event to Redis stream: %s", exc)

    flags_increment = int(flags_count or 0) if was_flagged else 0
    file_size_value = max(int(file_size), 0) if file_size else None
    file_size_sq = file_size_value * file_size_value if file_size_value is not None else None
    duration_value = max(int(scan_duration_ms), 0) if scan_duration_ms is not None else None
    duration_sq = duration_value * duration_value if duration_value is not None else None

    await _record_rollup(
        client,
        rollup_key_value=rollup_key_value,
        status_key=status_key,
        guild_id=guild_id,
        metric_date=metric_date,
        was_flagged=was_flagged,
        flags_increment=flags_increment,
        file_size=file_size_value,
        file_size_sq=file_size_sq,
        duration=duration_value,
        duration_sq=duration_sq,
        status=status,
        reference=reference,
        occurred_iso=occurred_iso,
        detail_json=detail_json,
        store_last_details=store_last_details,
        acceleration_prefix=acceleration_prefix,
    )

    if guild_id not in (None, 0):
        await _record_rollup(
            client,
            rollup_key_value=global_rollup_key_value,
            status_key=global_status_key,
            guild_id=None,
            metric_date=metric_date,
            was_flagged=was_flagged,
            flags_increment=flags_increment,
            file_size=file_size_value,
            file_size_sq=file_size_sq,
            duration=duration_value,
            duration_sq=duration_sq,
            status=status,
            reference=reference,
            occurred_iso=occurred_iso,
            detail_json=detail_json,
            store_last_details=store_last_details,
            acceleration_prefix=acceleration_prefix,
        )

    totals_hash = totals_key()
    totals_status_hash = totals_status_key()

    await _apply_metric_updates(
        client,
        totals_hash,
        was_flagged=was_flagged,
        flags_increment=flags_increment,
        file_size=file_size_value,
        file_size_sq=file_size_sq,
        duration=duration_value,
        duration_sq=duration_sq,
        status=status,
        reference=reference,
        occurred_iso=occurred_iso,
        detail_json=detail_json,
        store_last_details=store_last_details,
        acceleration_prefix=acceleration_prefix,
        set_updated_at=True,
    )

    await client.hincrby(totals_status_hash, status, 1)


async def _record_rollup(
    client: Any,
    *,
    rollup_key_value: str,
    status_key: str,
    guild_id: int | None,
    metric_date: date,
    was_flagged: bool,
    flags_increment: int,
    file_size: int | None,
    file_size_sq: int | None,
    duration: int | None,
    duration_sq: int | None,
    status: str,
    reference: str | None,
    occurred_iso: str,
    detail_json: str,
    store_last_details: bool,
    acceleration_prefix: str,
) -> None:
    await _apply_metric_updates(
        client,
        rollup_key_value,
        was_flagged=was_flagged,
        flags_increment=flags_increment,
        file_size=file_size,
        file_size_sq=file_size_sq,
        duration=duration,
        duration_sq=duration_sq,
        status=status,
        reference=reference,
        occurred_iso=occurred_iso,
        detail_json=detail_json,
        store_last_details=store_last_details,
        acceleration_prefix=acceleration_prefix,
        set_updated_at=True,
    )

    await client.hincrby(status_key, status, 1)
    await _index_rollup(client, rollup_key_value, guild_id, metric_date)


async def _apply_metric_updates(
    client: Any,
    hash_key: str,
    *,
    was_flagged: bool,
    flags_increment: int,
    file_size: int | None,
    file_size_sq: int | None,
    duration: int | None,
    duration_sq: int | None,
    status: str,
    reference: str | None,
    occurred_iso: str,
    detail_json: str,
    store_last_details: bool,
    acceleration_prefix: str,
    set_updated_at: bool,
) -> None:
    await client.hincrby(hash_key, "scans_count", 1)
    if was_flagged:
        await client.hincrby(hash_key, "flagged_count", 1)
    if flags_increment:
        await client.hincrby(hash_key, "flags_sum", flags_increment)
    if file_size is not None:
        await client.hincrby(hash_key, "total_bytes", file_size)
        if file_size_sq is not None:
            await client.hincrby(hash_key, "total_bytes_sq", file_size_sq)
    if duration is not None:
        await client.hincrby(hash_key, "total_duration_ms", duration)
        await client.hset(hash_key, mapping={"last_duration_ms": duration})
        if duration_sq is not None:
            await client.hincrby(hash_key, "total_duration_sq_ms", duration_sq)
    else:
        await client.hset(hash_key, mapping={"last_duration_ms": 0})

    await client.hset(hash_key, mapping={"last_status": status})
    if was_flagged:
        flag_mapping = {
            "last_flagged_at": occurred_iso,
            "last_reference": reference or "",
            "last_details": detail_json,
        }
        await client.hset(hash_key, mapping=flag_mapping)
    elif store_last_details:
        await client.hset(hash_key, mapping={"last_details": detail_json})

    if set_updated_at:
        await client.hset(
            hash_key,
            mapping={"updated_at": datetime.now(timezone.utc).isoformat()},
        )

    await _update_acceleration_bucket(
        client,
        hash_key,
        prefix=acceleration_prefix,
        was_flagged=was_flagged,
        flags_increment=flags_increment,
        file_size=file_size,
        file_size_sq=file_size_sq,
        duration=duration,
        duration_sq=duration_sq,
        status=status,
        reference=reference,
        occurred_iso=occurred_iso,
        detail_json=detail_json,
    )


async def _update_acceleration_bucket(
    client: Any,
    hash_key: str,
    *,
    prefix: str,
    was_flagged: bool,
    flags_increment: int,
    file_size: int | None,
    file_size_sq: int | None,
    duration: int | None,
    duration_sq: int | None,
    status: str,
    reference: str | None,
    occurred_iso: str,
    detail_json: str,
) -> None:
    if not prefix:
        return

    await client.hincrby(hash_key, f"{prefix}_scans_count", 1)
    if was_flagged:
        await client.hincrby(hash_key, f"{prefix}_flagged_count", 1)
    if flags_increment:
        await client.hincrby(hash_key, f"{prefix}_flags_sum", flags_increment)
    if file_size is not None:
        await client.hincrby(hash_key, f"{prefix}_total_bytes", file_size)
        if file_size_sq is not None:
            await client.hincrby(hash_key, f"{prefix}_total_bytes_sq", file_size_sq)
    if duration is not None:
        await client.hincrby(hash_key, f"{prefix}_total_duration_ms", duration)
        await client.hset(hash_key, mapping={f"{prefix}_last_duration_ms": duration})
        if duration_sq is not None:
            await client.hincrby(hash_key, f"{prefix}_total_duration_sq_ms", duration_sq)
    else:
        await client.hset(hash_key, mapping={f"{prefix}_last_duration_ms": 0})

    bucket_updates: dict[str, Any] = {
        f"{prefix}_last_at": occurred_iso,
        f"{prefix}_last_status": status,
        f"{prefix}_last_details": detail_json,
    }
    if reference is not None:
        bucket_updates[f"{prefix}_last_reference"] = reference
    if was_flagged:
        bucket_updates[f"{prefix}_last_flagged_at"] = occurred_iso
    await client.hset(hash_key, mapping=bucket_updates)


async def _index_rollup(client: Any, rollup_key_value: str, guild_id: int | None, metric_date: datetime.date) -> None:
    index_score = float(metric_date.toordinal())
    await client.zadd(rollup_index_key(), {rollup_key_value: index_score})
    await client.zadd(rollup_guild_index_key(guild_id), {rollup_key_value: index_score})


__all__ = ["accumulate_media_metric"]
