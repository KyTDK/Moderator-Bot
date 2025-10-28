"""Helpers for managing simple guild-scoped database lists.

These helpers centralize the small CRUD snippets that many cogs repeat
when storing guild-specific lists (for example banned words or URLs).
"""

from __future__ import annotations

from enum import Enum
from typing import List

from modules.utils.mysql import execute_query

_ALLOWED_IDENTIFIER_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")


def _validate_identifier(identifier: str, *, kind: str) -> None:
    if not identifier or any(ch not in _ALLOWED_IDENTIFIER_CHARS for ch in identifier):
        raise ValueError(f"Invalid {kind} name: {identifier!r}")


class GuildListAddResult(Enum):
    """Result of attempting to add a value to a guild list."""

    ADDED = "added"
    ALREADY_PRESENT = "already_present"
    LIMIT_REACHED = "limit_reached"


async def fetch_values(*, guild_id: int, table: str, column: str) -> List[str]:
    """Return all values stored for a guild."""

    _validate_identifier(table, kind="table")
    _validate_identifier(column, kind="column")

    rows, _ = await execute_query(
        f"SELECT {column} FROM {table} WHERE guild_id = %s",
        (guild_id,),
        fetch_all=True,
    )
    return [row[0] for row in (rows or []) if row]


async def add_value(
    *,
    guild_id: int,
    table: str,
    column: str,
    value: str,
    limit: int | None = None,
) -> GuildListAddResult:
    """Insert a new value into the guild list with optional limit checking."""

    _validate_identifier(table, kind="table")
    _validate_identifier(column, kind="column")

    if limit is not None:
        count_row, _ = await execute_query(
            f"SELECT COUNT(*) FROM {table} WHERE guild_id = %s",
            (guild_id,),
            fetch_one=True,
        )
        count = count_row[0] if count_row else 0
        if count >= limit:
            return GuildListAddResult.LIMIT_REACHED

    existing, _ = await execute_query(
        f"SELECT 1 FROM {table} WHERE guild_id = %s AND {column} = %s",
        (guild_id, value),
        fetch_one=True,
    )
    if existing:
        return GuildListAddResult.ALREADY_PRESENT

    await execute_query(
        f"INSERT INTO {table} (guild_id, {column}) VALUES (%s, %s)",
        (guild_id, value),
    )
    return GuildListAddResult.ADDED


async def remove_value(*, guild_id: int, table: str, column: str, value: str) -> bool:
    """Remove a value from the guild list. Returns True if anything was deleted."""

    _validate_identifier(table, kind="table")
    _validate_identifier(column, kind="column")

    _, affected = await execute_query(
        f"DELETE FROM {table} WHERE guild_id = %s AND {column} = %s",
        (guild_id, value),
    )
    return bool(affected)


async def clear_values(*, guild_id: int, table: str) -> int:
    """Clear all values stored for a guild. Returns the number of removed rows."""

    _validate_identifier(table, kind="table")

    _, affected = await execute_query(
        f"DELETE FROM {table} WHERE guild_id = %s",
        (guild_id,),
    )
    return affected or 0


async def count_values(*, guild_id: int, table: str) -> int:
    """Return the number of values stored for a guild."""

    _validate_identifier(table, kind="table")

    row, _ = await execute_query(
        f"SELECT COUNT(*) FROM {table} WHERE guild_id = %s",
        (guild_id,),
        fetch_one=True,
    )
    return row[0] if row else 0


__all__ = [
    "GuildListAddResult",
    "add_value",
    "clear_values",
    "count_values",
    "fetch_values",
    "remove_value",
]
