from .connection import execute_query

async def is_accelerated(user_id: int | None = None, guild_id: int | None = None) -> bool:
    """
    Return True if the user or guild should have Accelerated access.

    Rules:
    - status = 'active' AND (next_billing IS NULL OR next_billing > now)
    - status = 'cancelled' AND next_billing > now (honour end of billing)
    """
    conditions = []
    params: list[int] = []

    if guild_id:
        conditions.append("guild_id = %s")
        params.append(guild_id)
    if user_id:
        conditions.append("buyer_id = %s")
        params.append(user_id)

    if not conditions:
        return False

    where_clause = " AND ".join(conditions)

    query = f"""
        SELECT 1 FROM premium_guilds
        WHERE {where_clause}
          AND (
                (status = 'active' AND (next_billing IS NULL OR next_billing > UTC_TIMESTAMP()))
             OR (status = 'cancelled' AND next_billing > UTC_TIMESTAMP())
          )
        LIMIT 1
    """

    result, _ = await execute_query(query, tuple(params), fetch_one=True)
    return result is not None

async def get_premium_status(guild_id: int):
    """
    Return a dict with 'status' and 'next_billing' for a guild, or None if not found.
    """
    row, _ = await execute_query(
        "SELECT status, next_billing FROM premium_guilds WHERE guild_id = %s LIMIT 1",
        (guild_id,),
        fetch_one=True,
    )
    if not row:
        return None
    return {"status": row[0], "next_billing": row[1]}

async def add_guild(guild_id: int, name: str, owner_id: int):
    """
    Add a new guild to the database.
    """
    await execute_query(
        """
        INSERT INTO guilds (guild_id, name, owner_id)
        VALUES (%s, %s, %s)
        ON DUPLICATE KEY UPDATE name = VALUES(name), owner_id = VALUES(owner_id)
        """,
        (guild_id, name, owner_id),
    )

async def remove_guild(guild_id: int):
    """
    Remove a guild from the database.
    """
    await execute_query(
        "DELETE FROM guilds WHERE guild_id = %s",
        (guild_id,),
    )
