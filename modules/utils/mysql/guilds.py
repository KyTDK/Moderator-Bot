from __future__ import annotations

from typing import Optional

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


async def add_guild(guild_id: int, name: str, owner_id: int, locale: Optional[str]):
    """Add a new guild to the database."""

    await execute_query(
        """
        INSERT INTO guilds (guild_id, name, owner_id, locale)
        VALUES (%s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            name = VALUES(name),
            owner_id = VALUES(owner_id),
            locale = VALUES(locale)
        """,
        (guild_id, name, owner_id, locale),
    )


async def remove_guild(guild_id: int):
    """Remove a guild from the database."""

    await execute_query(
        "DELETE FROM guilds WHERE guild_id = %s",
        (guild_id,),
    )

