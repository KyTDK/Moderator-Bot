from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from ..connection import execute_query, get_pool
from .base import decode_json_map, ensure_naive


async def update_global_totals(
    *,
    occurred: datetime,
    status: str,
    was_flagged: bool,
    flags_increment: int,
    bytes_increment: int,
    duration_increment: int,
    reference: str | None,
    detail_json: str,
    store_last_details: bool,
) -> None:
    occurred_naive = ensure_naive(occurred)
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
                        last_status,
                        last_reference,
                        last_details
                    FROM moderation_metric_totals
                    WHERE singleton_id = 1
                    FOR UPDATE
                    """
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
                        _last_status,
                        last_reference,
                        last_details_raw,
                    ) = row

                    scans_count = int(scans_count_raw or 0) + 1
                    flagged_count = int(flagged_count_raw or 0) + (1 if was_flagged else 0)
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
                        UPDATE moderation_metric_totals
                        SET scans_count = %s,
                            flagged_count = %s,
                            flags_sum = %s,
                            total_bytes = %s,
                            total_duration_ms = %s,
                            status_counts = %s,
                            last_flagged_at = %s,
                            last_status = %s,
                            last_reference = %s,
                            last_details = %s
                        WHERE singleton_id = 1
                        """,
                        (
                            scans_count,
                            flagged_count,
                            flags_sum,
                            total_bytes,
                            total_duration_ms,
                            json.dumps(status_counts, ensure_ascii=False),
                            new_last_flagged_at,
                            status,
                            new_last_reference,
                            new_last_details,
                        ),
                    )
                else:
                    status_counts_json = json.dumps({status: 1}, ensure_ascii=False)
                    last_flagged_at_value = occurred_naive if was_flagged else None
                    last_reference_value = reference if was_flagged else None
                    last_details_value = detail_json if (was_flagged or store_last_details) else None
                    await cur.execute(
                        """
                        INSERT INTO moderation_metric_totals (
                            singleton_id,
                            scans_count,
                            flagged_count,
                            flags_sum,
                            total_bytes,
                            total_duration_ms,
                            status_counts,
                            last_flagged_at,
                            last_status,
                            last_reference,
                            last_details
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            1,
                            1,
                            1 if was_flagged else 0,
                            flags_increment,
                            bytes_increment,
                            duration_increment,
                            status_counts_json,
                            last_flagged_at_value,
                            status,
                            last_reference_value,
                            last_details_value,
                        ),
                    )
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise


async def fetch_metric_totals() -> dict[str, Any]:
    row, _ = await execute_query(
        """
        SELECT
            scans_count,
            flagged_count,
            flags_sum,
            total_bytes,
            total_duration_ms,
            status_counts,
            last_flagged_at,
            last_status,
            last_reference,
            last_details
        FROM moderation_metric_totals
        WHERE singleton_id = 1
        """,
        (),
        fetch_one=True,
    )
    if not row:
        return {
            "scans_count": 0,
            "flagged_count": 0,
            "flags_sum": 0,
            "total_bytes": 0,
            "total_duration_ms": 0,
            "status_counts": {},
            "last_flagged_at": None,
            "last_status": None,
            "last_reference": None,
            "last_details": {},
        }

    (
        scans_count,
        flagged_count,
        flags_sum,
        total_bytes,
        total_duration_ms,
        status_counts_raw,
        last_flagged_at,
        last_status,
        last_reference,
        last_details_raw,
    ) = row

    return {
        "scans_count": int(scans_count or 0),
        "flagged_count": int(flagged_count or 0),
        "flags_sum": int(flags_sum or 0),
        "total_bytes": int(total_bytes or 0),
        "total_duration_ms": int(total_duration_ms or 0),
        "status_counts": decode_json_map(status_counts_raw),
        "last_flagged_at": last_flagged_at.replace(tzinfo=timezone.utc)
        if isinstance(last_flagged_at, datetime)
        else last_flagged_at,
        "last_status": last_status,
        "last_reference": last_reference,
        "last_details": decode_json_map(last_details_raw),
    }
