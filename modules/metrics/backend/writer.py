from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from ..config import get_metrics_redis_config
from ._redis import RedisError, get_redis_client
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
) -> None:
    client = await get_redis_client()
    config = get_metrics_redis_config()

    occurred = ensure_utc(occurred_at)
    metric_date = occurred.date()
    rollup_key_value = rollup_key(metric_date, guild_id, content_type)
    status_key = rollup_status_key(rollup_key_value)

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
        "occurred_at": occurred.isoformat(),
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
    await client.hincrby(rollup_key_value, "scans_count", 1)
    if was_flagged:
        await client.hincrby(rollup_key_value, "flagged_count", 1)
    if flags_increment:
        await client.hincrby(rollup_key_value, "flags_sum", flags_increment)
    if file_size:
        await client.hincrby(rollup_key_value, "total_bytes", max(int(file_size), 0))

    if scan_duration_ms is not None:
        duration = max(int(scan_duration_ms), 0)
        await client.hincrby(rollup_key_value, "total_duration_ms", duration)
        await client.hset(rollup_key_value, mapping={"last_duration_ms": duration})
    else:
        await client.hset(rollup_key_value, mapping={"last_duration_ms": 0})

    await client.hset(rollup_key_value, mapping={"last_status": status})
    if was_flagged:
        await client.hset(
            rollup_key_value,
            mapping={
                "last_flagged_at": occurred.isoformat(),
                "last_reference": reference or "",
                "last_details": detail_json,
            },
        )
    elif store_last_details:
        await client.hset(rollup_key_value, mapping={"last_details": detail_json})

    await client.hincrby(status_key, status, 1)

    index_score = float(metric_date.toordinal())
    await client.zadd(rollup_index_key(), {rollup_key_value: index_score})
    await client.zadd(rollup_guild_index_key(guild_id), {rollup_key_value: index_score})

    totals_hash = totals_key()
    totals_status_hash = totals_status_key()

    await client.hincrby(totals_hash, "scans_count", 1)
    if was_flagged:
        await client.hincrby(totals_hash, "flagged_count", 1)
    if flags_increment:
        await client.hincrby(totals_hash, "flags_sum", flags_increment)
    if file_size:
        await client.hincrby(totals_hash, "total_bytes", max(int(file_size), 0))
    if scan_duration_ms is not None:
        duration = max(int(scan_duration_ms), 0)
        await client.hincrby(totals_hash, "total_duration_ms", duration)
        await client.hset(totals_hash, mapping={"last_duration_ms": duration})
    else:
        await client.hset(totals_hash, mapping={"last_duration_ms": 0})

    await client.hset(totals_hash, mapping={"last_status": status})
    if was_flagged:
        await client.hset(
            totals_hash,
            mapping={
                "last_flagged_at": occurred.isoformat(),
                "last_reference": reference or "",
                "last_details": detail_json,
            },
        )
    elif store_last_details:
        await client.hset(totals_hash, mapping={"last_details": detail_json})

    await client.hset(
        totals_hash,
        mapping={"updated_at": datetime.now(timezone.utc).isoformat()},
    )
    await client.hincrby(totals_status_hash, status, 1)


__all__ = ["accumulate_media_metric"]
