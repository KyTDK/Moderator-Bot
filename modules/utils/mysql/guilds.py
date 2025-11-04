from __future__ import annotations

from typing import Dict, Optional

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
        VALUES (%s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            name = VALUES(name),
            owner_id = VALUES(owner_id),
            locale = VALUES(locale),
            total_members = VALUES(total_members)
        """,
        (guild_id, name, owner_id, locale, members_value),
    )


async def remove_guild(guild_id: int):
    """Remove a guild from the database."""

    await execute_query(
        "DELETE FROM guilds WHERE guild_id = %s",
        (guild_id,),
    )
