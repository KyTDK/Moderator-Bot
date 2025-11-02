from .connection import execute_query

async def cleanup_orphaned_guilds(active_guild_ids):
    """Remove database records for guilds the bot is no longer in."""
    if not active_guild_ids:
        print("[cleanup] No active guild IDs provided.")
        return

    placeholders = ",".join(["%s"] * len(active_guild_ids))
    query = f"SELECT DISTINCT guild_id FROM settings WHERE guild_id NOT IN ({placeholders})"
    rows, _ = await execute_query(query, tuple(active_guild_ids), fetch_all=True)

    if not rows:
        print("[cleanup] No orphaned guilds found.")
        return

    guild_ids = [r[0] for r in rows]
    tables = [
        "settings",
        "banned_words",
        "timeouts",
        "scam_messages",
        "scam_urls",
        "strikes",
        "api_pool",
        "aimod_usage",
        "vcmod_usage",
    ]

    total_deleted = 0
    for gid in guild_ids:
        print(f"[cleanup] Checking orphaned guild data for: {gid}")
        for table in tables:
            _, affected = await execute_query(f"DELETE FROM {table} WHERE guild_id = %s", (gid,))
            if affected > 0:
                print(f"[cleanup] + Deleted {affected} rows from {table} for guild {gid}")
                total_deleted += affected

    if total_deleted == 0:
        print("[cleanup] Cleanup performed, but nothing to delete.")
    else:
        print(f"[cleanup] Completed. Total rows deleted: {total_deleted}")
