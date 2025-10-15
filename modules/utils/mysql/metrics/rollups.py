from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any

from ..connection import execute_query, get_pool
from .base import decode_json_map, ensure_naive, ensure_utc, normalise_since
from .totals import update_global_totals


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
    occurred = ensure_utc(occurred_at)
    metric_date = occurred.date()
    occurred_naive = ensure_naive(occurred)

    guild_key = int(guild_id or 0)
    flagged_increment = 1 if was_flagged else 0
    flags_increment = int(flags_count if was_flagged else 0)
    bytes_increment = max(int(file_size or 0), 0)
    duration_increment = max(int(scan_duration_ms or 0), 0)

    detail_payload: dict[str, Any] = {}
    if details:
        detail_payload.update(details)
    detail_payload.setdefault("status", status)
    detail_payload.setdefault("was_flagged", was_flagged)
    if reference is not None:
        detail_payload.setdefault("reference", reference)
    detail_json = json.dumps(detail_payload, ensure_ascii=False)

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.begin()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT
                        scans_count,
                        flagged_count,
                        flags_sum,
                        total_bytes,
                        total_duration_ms,
                        status_counts,
                        last_flagged_at,
                        last_reference,
                        last_status,
                        last_details
                    FROM moderation_metric_rollups
                    WHERE metric_date = %s
                      AND guild_id = %s
                      AND content_type = %s
                    FOR UPDATE
                    """,
                    (metric_date, guild_key, content_type),
                )
                row = await cur.fetchone()
                if row:
                    (
                        scans_count_raw,
                        flagged_count_raw,
                        flags_sum_raw,
                        total_bytes_raw,
                        total_duration_raw,
                        status_counts_raw,
                        last_flagged_at,
                        last_reference,
                        _last_status,
                        last_details_raw,
                    ) = row

                    scans_count = int(scans_count_raw or 0) + 1
                    flagged_count = int(flagged_count_raw or 0) + flagged_increment
                    flags_sum = int(flags_sum_raw or 0) + flags_increment
                    total_bytes = int(total_bytes_raw or 0) + bytes_increment
                    total_duration_ms = int(total_duration_raw or 0) + duration_increment

                    status_counts = decode_json_map(status_counts_raw)
                    status_counts[status] = int(status_counts.get(status, 0) or 0) + 1

                    new_last_flagged_at = last_flagged_at
                    new_last_reference = last_reference
                    new_last_details = last_details_raw

                    if was_flagged:
                        new_last_flagged_at = occurred_naive
                        new_last_reference = reference
                        new_last_details = detail_json
                    elif store_last_details:
                        new_last_details = detail_json

                    await cur.execute(
                        """
                        UPDATE moderation_metric_rollups
                        SET scans_count = %s,
                            flagged_count = %s,
                            flags_sum = %s,
                            total_bytes = %s,
                            total_duration_ms = %s,
                            status_counts = %s,
                            last_flagged_at = %s,
                            last_reference = %s,
                            last_status = %s,
                            last_details = %s
                        WHERE metric_date = %s
                          AND guild_id = %s
                          AND content_type = %s
                        """,
                        (
                            scans_count,
                            flagged_count,
                            flags_sum,
                            total_bytes,
                            total_duration_ms,
                            json.dumps(status_counts, ensure_ascii=False),
                            new_last_flagged_at,
                            new_last_reference,
                            status,
                            new_last_details,
                            metric_date,
                            guild_key,
                            content_type,
                        ),
                    )
                else:
                    status_counts_json = json.dumps({status: 1}, ensure_ascii=False)
                    last_flagged_at_value = occurred_naive if was_flagged else None
                    last_reference_value = reference if was_flagged else None
                    last_details_value = detail_json if (was_flagged or store_last_details) else None
                    await cur.execute(
                        """
                        INSERT INTO moderation_metric_rollups (
                            metric_date,
                            guild_id,
                            content_type,
                            scans_count,
                            flagged_count,
                            flags_sum,
                            total_bytes,
                            total_duration_ms,
                            last_flagged_at,
                            last_reference,
                            last_status,
                            status_counts,
                            last_details
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            metric_date,
                            guild_key,
                            content_type,
                            1,
                            flagged_increment,
                            flags_increment,
                            bytes_increment,
                            duration_increment,
                            last_flagged_at_value,
                            last_reference_value,
                            status,
                            status_counts_json,
                            last_details_value,
                        ),
                    )
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise

    await update_global_totals(
        occurred=occurred,
        status=status,
        was_flagged=was_flagged,
        flags_increment=flags_increment,
        bytes_increment=bytes_increment,
        duration_increment=duration_increment,
        reference=reference,
        detail_json=detail_json,
        store_last_details=store_last_details,
    )


async def fetch_metric_rollups(
    *,
    guild_id: int | None = None,
    content_type: str | None = None,
    since: date | datetime | None = None,
    limit: int = 30,
) -> list[dict[str, Any]]:
    where_clauses: list[str] = []
    params: list[Any] = []
    if guild_id is not None:
        where_clauses.append("guild_id = %s")
        params.append(int(guild_id))
    if content_type:
        where_clauses.append("content_type = %s")
        params.append(content_type)
    since_date = normalise_since(since)
    if since_date is not None:
        where_clauses.append("metric_date >= %s")
        params.append(since_date)

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    params_with_limit = tuple(params + [int(limit)])

    rows, _ = await execute_query(
        f"""
        SELECT
            metric_date,
            guild_id,
            content_type,
            scans_count,
            flagged_count,
            flags_sum,
            total_bytes,
            total_duration_ms,
            last_flagged_at,
            last_reference,
            last_status,
            status_counts,
            last_details
        FROM moderation_metric_rollups
        {where_sql}
        ORDER BY metric_date DESC
        LIMIT %s
        """,
        params_with_limit,
        fetch_all=True,
    )

    rollups: list[dict[str, Any]] = []
    for row in rows or []:
        (
            metric_date,
            guild_raw,
            content,
            scans_count,
            flagged_count,
            flags_sum,
            total_bytes,
            total_duration_ms,
            last_flagged_at,
            last_reference,
            last_status,
            status_counts_raw,
            last_details_raw,
        ) = row
        rollups.append(
            {
                "metric_date": metric_date,
                "guild_id": None if guild_raw in (None, 0) else int(guild_raw),
                "content_type": content,
                "scans_count": int(scans_count or 0),
                "flagged_count": int(flagged_count or 0),
                "flags_sum": int(flags_sum or 0),
                "total_bytes": int(total_bytes or 0),
                "total_duration_ms": int(total_duration_ms or 0),
                "last_flagged_at": last_flagged_at.replace(tzinfo=timezone.utc)
                if isinstance(last_flagged_at, datetime)
                else last_flagged_at,
                "last_reference": last_reference,
                "last_status": last_status,
                "status_counts": decode_json_map(status_counts_raw),
                "last_details": decode_json_map(last_details_raw),
            }
        )
    return rollups


async def summarise_rollups(
    *,
    guild_id: int | None = None,
    since: date | datetime | None = None,
) -> list[dict[str, Any]]:
    where_clauses: list[str] = []
    params: list[Any] = []
    if guild_id is not None:
        where_clauses.append("guild_id = %s")
        params.append(int(guild_id))
    since_date = normalise_since(since)
    if since_date is not None:
        where_clauses.append("metric_date >= %s")
        params.append(since_date)

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    rows, _ = await execute_query(
        f"""
        SELECT
            content_type,
            SUM(scans_count) AS scans,
            SUM(flagged_count) AS flagged,
            SUM(flags_sum) AS flags_sum,
            SUM(total_bytes) AS bytes_total,
            SUM(total_duration_ms) AS duration_total
        FROM moderation_metric_rollups
        {where_sql}
        GROUP BY content_type
        ORDER BY scans DESC
        """,
        tuple(params),
        fetch_all=True,
    )

    summary: list[dict[str, Any]] = []
    for row in rows or []:
        (
            content_type,
            scans,
            flagged,
            flags_sum,
            bytes_total,
            duration_total,
        ) = row
        summary.append(
            {
                "content_type": content_type,
                "scans": int(scans or 0),
                "flagged": int(flagged or 0),
                "flags_sum": int(flags_sum or 0),
                "bytes_total": int(bytes_total or 0),
                "duration_total_ms": int(duration_total or 0),
            }
        )
    return summary
