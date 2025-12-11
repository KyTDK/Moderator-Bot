from __future__ import annotations

from typing import Dict, Optional, Set

from .connection import execute_query


async def get_guild_locale(guild_id: int) -> Optional[str]:
    """Return the stored locale override for *guild_id* if one exists."""

    row, _ = await execute_query(
        "SELECT locale FROM guilds WHERE guild_id = %s LIMIT 1",
        (guild_id,),
        fetch_one=True,
    )

    if not row:
        return None

    locale = row[0]
    return str(locale) if locale is not None else None


async def get_all_guild_locales() -> Dict[int, Optional[str]]:
    """Return a mapping of guild IDs to their stored locale values."""

    rows, _ = await execute_query(
        "SELECT guild_id, locale FROM guilds",
        fetch_all=True,
    )

    if not rows:
        return {}

    result: Dict[int, Optional[str]] = {}
    for guild_id, locale in rows:
        normalized_locale = str(locale) if locale is not None else None
        try:
            result[int(guild_id)] = normalized_locale
        except (TypeError, ValueError):
            # Skip rows with malformed guild IDs but continue processing others.
            continue

    return result


async def add_guild(
    guild_id: int,
    name: str,
    owner_id: int,
    locale: Optional[str],
    total_members: Optional[int],
) -> None:
    """Add a new guild to the database."""

    members_value = 0
    if total_members is not None:
        try:
            members_value = max(int(total_members), 0)
        except (TypeError, ValueError):
            members_value = 0

    await execute_query(
        """
        INSERT INTO guilds (guild_id, name, owner_id, locale, total_members)
        VALUES (%s, %s, %s, %s, %s) AS new_values
        ON DUPLICATE KEY UPDATE
            name = new_values.name,
            owner_id = new_values.owner_id,
            locale = new_values.locale,
            total_members = new_values.total_members
        """,
        (guild_id, name, owner_id, locale, members_value),
    )


async def remove_guild(guild_id: int):
    """Remove a guild from the database."""

    await execute_query(
        "DELETE FROM guilds WHERE guild_id = %s",
        (guild_id,),
    )


async def get_banned_guild_ids() -> Set[int]:
    """Return the IDs of guilds that are not allowed to use the bot."""

    rows, _ = await execute_query(
        "SELECT guild_id FROM banned_guilds",
        fetch_all=True,
    )

    if not rows:
        return set()

    banned_ids: Set[int] = set()
    for (guild_id,) in rows:
        try:
            banned_ids.add(int(guild_id))
        except (TypeError, ValueError):
            continue

    return banned_ids


async def is_guild_banned(guild_id: int) -> bool:
    """Return True if *guild_id* is marked as banned."""

    row, _ = await execute_query(
        "SELECT 1 FROM banned_guilds WHERE guild_id = %s LIMIT 1",
        (guild_id,),
        fetch_one=True,
    )

    return bool(row)


async def get_banned_guild_reason(guild_id: int) -> Optional[str]:
    """Return the stored ban reason for *guild_id*, if present."""

    row, _ = await execute_query(
        "SELECT reason FROM banned_guilds WHERE guild_id = %s LIMIT 1",
        (guild_id,),
        fetch_one=True,
    )

    if not row:
        return None

    reason = row[0]
    return str(reason) if reason is not None else None


async def ban_guild(guild_id: int, reason: Optional[str]) -> None:
    """Add or update a banned guild entry."""

    await execute_query(
        """
        INSERT INTO banned_guilds (guild_id, reason)
        VALUES (%s, %s) AS new_values
        ON DUPLICATE KEY UPDATE reason = new_values.reason, added_at = CURRENT_TIMESTAMP
        """,
        (guild_id, reason),
    )


async def unban_guild(guild_id: int) -> bool:
    """Remove a guild from the banned list. Returns True if a row was deleted."""

    _, affected = await execute_query(
        "DELETE FROM banned_guilds WHERE guild_id = %s",
        (guild_id,),
    )
    return affected > 0
