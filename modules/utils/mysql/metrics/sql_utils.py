from __future__ import annotations

from collections.abc import Iterable
from typing import Sequence, Tuple

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
