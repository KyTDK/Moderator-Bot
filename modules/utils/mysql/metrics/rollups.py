from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

import aiomysql

from ..connection import execute_query, get_pool
from .base import (
    MetricRow,
    build_metric_update,
    ensure_naive,
    ensure_utc,
    normalise_since,
)
from .sql_utils import (
    ROLLUP_DIMENSION_COLUMNS,
    ROLLUP_VALUE_COLUMNS,
    insert_columns_clause,
    select_columns_clause,
    update_assignments_clause,
    run_transaction_with_retry,
)
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
    metric_update = build_metric_update(
        status=status,
        was_flagged=was_flagged,
        flags_increment=flags_increment,
        bytes_increment=bytes_increment,
        duration_increment=duration_increment,
        reference=reference,
        detail_json=detail_json,
        store_last_details=store_last_details,
        occurred_at=occurred_naive,
    )

    value_select_clause = select_columns_clause(ROLLUP_VALUE_COLUMNS)
    update_clause = update_assignments_clause(ROLLUP_VALUE_COLUMNS)
    insert_columns_sql, insert_placeholders = insert_columns_clause(
        ROLLUP_DIMENSION_COLUMNS,
        ROLLUP_VALUE_COLUMNS,
    )

    async def _execute(conn: aiomysql.Connection) -> None:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT
                    {value_select_clause}
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
                state = MetricRow.from_db_row(row)
                state.apply_update(metric_update)
                await cur.execute(
                    f"""
                    UPDATE moderation_metric_rollups
                    SET {update_clause}
                    WHERE metric_date = %s
                      AND guild_id = %s
                      AND content_type = %s
                    """,
                    (
                        *state.as_update_tuple(),
                        metric_date,
                        guild_key,
                        content_type,
                    ),
                )
            else:
                state = MetricRow.empty()
                state.apply_update(metric_update)
                await cur.execute(
                    f"""
                    INSERT INTO moderation_metric_rollups (
                        {insert_columns_sql}
                    )
                    VALUES ({insert_placeholders})
                    """,
                    (
                        metric_date,
                        guild_key,
                        content_type,
                        *state.as_insert_tuple(),
                    ),
                )

    await run_transaction_with_retry(pool, _execute)

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

    value_select_clause = select_columns_clause(ROLLUP_VALUE_COLUMNS)
    rows, _ = await execute_query(
        f"""
        SELECT
            metric_date,
            guild_id,
            content_type,
            {value_select_clause}
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
        metric_date_value, guild_raw, content = row[:3]
        state = MetricRow.from_db_row(row[3:])
        rollup_payload = state.to_public_dict()
        rollup_payload.update(
            {
                "metric_date": metric_date_value,
                "guild_id": None if guild_raw in (None, 0) else int(guild_raw),
                "content_type": content,
            }
        )
        rollups.append(rollup_payload)
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
