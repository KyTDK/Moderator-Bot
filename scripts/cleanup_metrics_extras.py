from __future__ import annotations

import asyncio
import json
from typing import Any, Iterable, Mapping

from redis.asyncio import Redis

from modules.metrics.backend.acceleration import ACCELERATION_PREFIXES
from modules.metrics.config import get_metrics_redis_config
from modules.metrics.sanitizer import sanitize_details_blob

ROLLUP_BASE_FIELDS: set[str] = {
    "metric_date",
    "scans_count",
    "flagged_count",
    "flags_sum",
    "total_bytes",
    "total_bytes_sq",
    "total_duration_ms",
    "total_duration_sq_ms",
    "last_duration_ms",
    "total_frames_scanned",
    "total_frames_target",
    "last_status",
    "last_flagged_at",
    "last_details",
    "updated_at",
}

TOTALS_BASE_FIELDS: set[str] = {
    "scans_count",
    "flagged_count",
    "flags_sum",
    "total_bytes",
    "total_bytes_sq",
    "total_duration_ms",
    "total_duration_sq_ms",
    "last_duration_ms",
    "total_frames_scanned",
    "total_frames_target",
    "last_status",
    "last_flagged_at",
    "last_details",
    "updated_at",
}


def _acceleration_fields() -> set[str]:
    fields: set[str] = set()
    for prefix in ACCELERATION_PREFIXES.values():
        fields.update(
            {
                f"{prefix}_scans_count",
                f"{prefix}_flagged_count",
                f"{prefix}_flags_sum",
                f"{prefix}_total_bytes",
                f"{prefix}_total_bytes_sq",
                f"{prefix}_total_duration_ms",
                f"{prefix}_total_duration_sq_ms",
                f"{prefix}_last_duration_ms",
                f"{prefix}_total_frames_scanned",
                f"{prefix}_total_frames_target",
                f"{prefix}_last_status",
                f"{prefix}_last_flagged_at",
                f"{prefix}_last_at",
                f"{prefix}_last_details",
            }
        )
    return fields


ROLLUP_ALLOWED_FIELDS = ROLLUP_BASE_FIELDS | _acceleration_fields()
TOTALS_ALLOWED_FIELDS = TOTALS_BASE_FIELDS | _acceleration_fields()


async def _cleanup_hash(
    client: Redis,
    key: str,
    *,
    allowed_fields: set[str],
    detail_fields: Iterable[str],
) -> tuple[int, int]:
    removed = 0
    sanitized = 0

    current_fields = await client.hkeys(key)
    extras = [field for field in current_fields if field not in allowed_fields]
    if extras:
        removed = await client.hdel(key, *extras)

    for field in detail_fields:
        sanitized += await _sanitize_detail_field(client, key, field)

    return removed, sanitized


async def _sanitize_detail_field(client: Redis, key: str, field: str) -> int:
    raw = await client.hget(key, field)
    if raw is None:
        return 0

    payload = _safe_json_loads(raw)
    sanitized = sanitize_details_blob(payload)
    if not sanitized:
        new_value = ""
    else:
        new_value = json.dumps(sanitized, ensure_ascii=False)

    if new_value == (raw or ""):
        return 0

    await client.hset(key, mapping={field: new_value})
    return 1


def _safe_json_loads(raw: Any) -> Mapping[str, Any]:
    if not raw:
        return {}
    if isinstance(raw, Mapping):
        return raw
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    if isinstance(parsed, Mapping):
        return parsed
    return {}


async def cleanup_metrics_extras() -> None:
    config = get_metrics_redis_config()

    client = Redis(host="127.0.0.1", port=6379, db=1, decode_responses=True)

    try:
        rollup_removed = 0
        rollup_sanitized = 0
        detail_fields = _detail_fields_for_key()
        cursor = 0
        pattern = f"{config.key_prefix}:rollup:*"
        while True:
            cursor, keys = await client.scan(cursor=cursor, match=pattern, count=250)
            for key in keys:
                if key.endswith(":status"):
                    continue
                removed, sanitized = await _cleanup_hash(
                    client,
                    key,
                    allowed_fields=ROLLUP_ALLOWED_FIELDS,
                    detail_fields=detail_fields,
                )
                rollup_removed += removed
                rollup_sanitized += sanitized
            if cursor == 0:
                break

        totals_removed = 0
        totals_sanitized = 0
        totals_key = f"{config.key_prefix}:totals"
        removed, sanitized = await _cleanup_hash(
            client,
            totals_key,
            allowed_fields=TOTALS_ALLOWED_FIELDS,
            detail_fields=detail_fields,
        )
        totals_removed += removed
        totals_sanitized += sanitized

        print(
            "Cleaned metrics hashes:",
            f"rollups fields removed={rollup_removed}",
            f"rollups details sanitized={rollup_sanitized}",
            f"totals fields removed={totals_removed}",
            f"totals details sanitized={totals_sanitized}",
        )
    finally:
        await client.aclose()
        await client.connection_pool.disconnect()


_detail_cache: tuple[str, ...] | None = None


def _detail_fields_for_key() -> tuple[str, ...]:
    global _detail_cache
    if _detail_cache is None:
        fields = ["last_details"]
        for prefix in ACCELERATION_PREFIXES.values():
            fields.append(f"{prefix}_last_details")
        _detail_cache = tuple(fields)
    return _detail_cache


async def main() -> None:
    await cleanup_metrics_extras()


if __name__ == "__main__":
    asyncio.run(main())
