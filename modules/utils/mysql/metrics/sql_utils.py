from __future__ import annotations

import asyncio
import random
from typing import Awaitable, Callable, Sequence, Tuple

import aiomysql

from .base import AGGREGATE_COLUMN_NAMES

ROLLUP_DIMENSION_COLUMNS: Tuple[str, ...] = ("metric_date", "guild_id", "content_type")
ROLLUP_VALUE_COLUMNS: Tuple[str, ...] = (
    *AGGREGATE_COLUMN_NAMES,
    "last_flagged_at",
    "last_reference",
    "last_status",
    "status_counts",
    "last_details",
)

TOTALS_DIMENSION_COLUMNS: Tuple[str, ...] = ("singleton_id",)
TOTALS_VALUE_COLUMNS: Tuple[str, ...] = (
    *AGGREGATE_COLUMN_NAMES,
    "last_flagged_at",
    "last_reference",
    "last_status",
    "status_counts",
    "last_details",
)


def select_columns_clause(columns: Sequence[str]) -> str:
    joined = ",\n                        ".join(columns)
    return joined


def update_assignments_clause(columns: Sequence[str]) -> str:
    assignments = ",\n                            ".join(f"{name} = %s" for name in columns)
    return assignments


def insert_columns_clause(
    dimension_columns: Sequence[str],
    value_columns: Sequence[str],
) -> tuple[str, str]:
    all_columns = tuple(dimension_columns) + tuple(value_columns)
    column_sql = ", ".join(all_columns)
    placeholders = ", ".join(["%s"] * len(all_columns))
    return column_sql, placeholders


DEADLOCK_ERRORS = {1205, 1213}


async def run_transaction_with_retry(
    pool: aiomysql.Pool,
    work: Callable[[aiomysql.Connection], Awaitable[None]],
    *,
    max_attempts: int = 5,
    base_delay: float = 0.05,
    backoff: float = 2.0,
    jitter: float = 0.05,
) -> None:
    attempt = 0
    while True:
        async with pool.acquire() as conn:
            try:
                await conn.begin()
                await work(conn)
                await conn.commit()
                return
            except aiomysql.Error as exc:
                await conn.rollback()
                error_code = exc.args[0] if exc.args else None
                if (
                    error_code in DEADLOCK_ERRORS
                    and attempt < max_attempts - 1
                ):
                    delay = base_delay * (backoff ** attempt)
                    if jitter:
                        delay += random.uniform(0, jitter)
                    attempt += 1
                    await asyncio.sleep(delay)
                    continue
                raise
            except Exception:
                await conn.rollback()
                raise
