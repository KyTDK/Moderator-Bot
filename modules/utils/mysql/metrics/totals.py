from __future__ import annotations

from datetime import datetime
from typing import Any

from ..connection import execute_query, get_pool
from .base import MetricRow, build_metric_update, ensure_naive
from .sql_utils import (
    TOTALS_DIMENSION_COLUMNS,
    TOTALS_VALUE_COLUMNS,
    insert_columns_clause,
    select_columns_clause,
    update_assignments_clause,
)


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

    value_select_clause = select_columns_clause(TOTALS_VALUE_COLUMNS)
    update_clause = update_assignments_clause(TOTALS_VALUE_COLUMNS)
    insert_columns_sql, insert_placeholders = insert_columns_clause(
        TOTALS_DIMENSION_COLUMNS,
        TOTALS_VALUE_COLUMNS,
    )

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.begin()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"""
                    SELECT
                        {value_select_clause}
                    FROM moderation_metric_totals
                    WHERE singleton_id = 1
                    FOR UPDATE
                    """
                )
                row = await cur.fetchone()
                if row:
                    state = MetricRow.from_db_row(row)
                    state.apply_update(metric_update)
                    await cur.execute(
                        f"""
                        UPDATE moderation_metric_totals
                        SET {update_clause}
                        WHERE singleton_id = 1
                        """,
                        (
                            *state.as_update_tuple(),
                        ),
                    )
                else:
                    state = MetricRow.empty()
                    state.apply_update(metric_update)
                    await cur.execute(
                        f"""
                        INSERT INTO moderation_metric_totals (
                            {insert_columns_sql}
                        )
                        VALUES ({insert_placeholders})
                        """,
                        (
                            1,
                            *state.as_insert_tuple(),
                        ),
                    )
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise


async def fetch_metric_totals() -> dict[str, Any]:
    value_select_clause = select_columns_clause(TOTALS_VALUE_COLUMNS)
    row, _ = await execute_query(
        f"""
        SELECT
            {value_select_clause}
        FROM moderation_metric_totals
        WHERE singleton_id = 1
        """,
        (),
        fetch_one=True,
    )
    if not row:
        return MetricRow.empty().to_public_dict()

    state = MetricRow.from_db_row(row)
    return state.to_public_dict()
