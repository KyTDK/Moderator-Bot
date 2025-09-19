from .connection import execute_query

async def get_strike_count(user_id: int, guild_id: int) -> int:
    result, _ = await execute_query(
        """
        SELECT COUNT(*)
        FROM strikes
        WHERE user_id = %s
          AND guild_id = %s
          AND (expires_at IS NULL OR expires_at > UTC_TIMESTAMP())
        """,
        (user_id, guild_id),
        fetch_one=True,
    )
    return result[0] if result else 0

async def get_strikes(user_id: int, guild_id: int):
    strikes, _ = await execute_query(
        """
        SELECT id, reason, striked_by_id, timestamp, expires_at
        FROM strikes
        WHERE user_id = %s
          AND guild_id = %s
          AND (expires_at IS NULL OR expires_at > UTC_TIMESTAMP())
        ORDER BY timestamp DESC
        """,
        (user_id, guild_id),
        fetch_all=True,
    )
    return strikes

async def cleanup_expired_strikes():
    """
    Delete expired strikes from the database (where expires_at < now).
    """
    _, affected = await execute_query(
        """
        DELETE FROM strikes
        WHERE expires_at IS NOT NULL
          AND expires_at <= UTC_TIMESTAMP()
        """
    )
    if affected > 0:
        print(f"[strikes cleanup] Deleted {affected} expired strikes.")
    else:
        print("[strikes cleanup] No expired strikes to delete.")
    return affected
