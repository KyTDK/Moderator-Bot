from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any, Mapping

from modules.metrics import backend
from modules.utils.mysql.connection import execute_query, init_pool


async def _load_rollups() -> list[tuple[Any, ...]]:
    rows, _ = await execute_query(
        """
        SELECT
            metric_date,
            guild_id,
            content_type,
            scans_count,
            flagged_count,
            flags_sum,
            total_bytes,
            total_duration_ms,
            last_duration_ms,
            last_flagged_at,
            last_reference,
            last_status,
            status_counts,
            last_details
        FROM moderation_metric_rollups
        """,
        (),
        fetch_all=True,
        commit=False,
    )
    return list(rows or [])


async def _load_totals() -> Mapping[str, Any] | None:
    row, _ = await execute_query(
        """
        SELECT
            scans_count,
            flagged_count,
            flags_sum,
            total_bytes,
            total_duration_ms,
            last_duration_ms,
            last_flagged_at,
            last_reference,
            last_status,
            status_counts,
            last_details,
            updated_at
        FROM moderation_metric_totals
        WHERE singleton_id = 1
        """,
        (),
        fetch_one=True,
        commit=False,
    )
    if not row:
        return None
    (
        scans_count,
        flagged_count,
        flags_sum,
        total_bytes,
        total_duration_ms,
        last_duration_ms,
        last_flagged_at,
        last_reference,
        last_status,
        status_counts,
        last_details,
        updated_at,
    ) = row
    return {
        "scans_count": int(scans_count or 0),
        "flagged_count": int(flagged_count or 0),
        "flags_sum": int(flags_sum or 0),
        "total_bytes": int(total_bytes or 0),
        "total_duration_ms": int(total_duration_ms or 0),
        "last_duration_ms": int(last_duration_ms or 0),
        "last_flagged_at": last_flagged_at,
        "last_reference": last_reference,
        "last_status": last_status,
        "status_counts": json.loads(status_counts) if status_counts else {},
        "last_details": json.loads(last_details) if last_details else {},
        "updated_at": updated_at,
    }


async def migrate_metrics() -> None:
    await init_pool()

    rollup_rows = await _load_rollups()
    for (
        metric_date,
        guild_id,
        content_type,
        scans_count,
        flagged_count,
        flags_sum,
        total_bytes,
        total_duration_ms,
        last_duration_ms,
        last_flagged_at,
        last_reference,
        last_status,
        status_counts,
        last_details,
    ) in rollup_rows:
        await backend.import_rollup_snapshot(
            metric_date=metric_date,
            guild_id=None if guild_id in (None, 0) else int(guild_id),
            content_type=content_type,
            aggregates={
                "scans_count": int(scans_count or 0),
                "flagged_count": int(flagged_count or 0),
                "flags_sum": int(flags_sum or 0),
                "total_bytes": int(total_bytes or 0),
                "total_duration_ms": int(total_duration_ms or 0),
                "last_duration_ms": int(last_duration_ms or 0),
            },
            status_counts=json.loads(status_counts) if status_counts else {},
            last_flagged_at=last_flagged_at if isinstance(last_flagged_at, datetime) else None,
            last_status=last_status,
            last_reference=last_reference,
            last_details=json.loads(last_details) if last_details else {},
        )

    totals = await _load_totals()
    if totals:
        await backend.import_totals_snapshot(
            aggregates={
                "scans_count": totals["scans_count"],
                "flagged_count": totals["flagged_count"],
                "flags_sum": totals["flags_sum"],
                "total_bytes": totals["total_bytes"],
                "total_duration_ms": totals["total_duration_ms"],
                "last_duration_ms": totals["last_duration_ms"],
            },
            status_counts=totals["status_counts"],
            last_flagged_at=totals["last_flagged_at"],
            last_status=totals["last_status"],
            last_reference=totals["last_reference"],
            last_details=totals["last_details"],
            updated_at=totals["updated_at"],
        )

    # Drop legacy tables once all state has been imported.
    await execute_query("DROP TABLE IF EXISTS moderation_metric_rollups", (), commit=True)
    await execute_query("DROP TABLE IF EXISTS moderation_metric_totals", (), commit=True)


async def main() -> None:
    await migrate_metrics()
    print("Metrics successfully migrated to Redis and legacy MySQL tables dropped.")


if __name__ == "__main__":
    asyncio.run(main())
