import math
from datetime import datetime, timezone

from modules.ai.costs import DEFAULT_BUDGET_LIMIT_USD

from .connection import execute_query
from .premium import get_premium_status

async def _get_current_cycle_end(guild_id: int) -> datetime:
    now = datetime.now(timezone.utc)
    status = await get_premium_status(guild_id)
    nb = status.get("next_billing") if status else None

    if isinstance(nb, str):
        try:
            nb = datetime.strptime(nb, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            nb = None
    elif isinstance(nb, datetime):
        nb = nb if nb.tzinfo else nb.replace(tzinfo=timezone.utc)
    else:
        nb = None

    if nb and nb > now:
        return nb

    year, month = now.year, now.month
    if month == 12:
        year, month = year + 1, 1
    else:
        month += 1
    return datetime(year, month, 1, tzinfo=timezone.utc)

async def get_aimod_usage(guild_id: int):
    """
    Get or initialize the current billing cycle usage for AI moderation.
    Single row per guild_id; on cycle rollover, counters reset and cycle_end updates.
    Returns a dict with tokens_used, cost_usd, limit_usd, cycle_end.
    """
    target_end = await _get_current_cycle_end(guild_id)
    # Fetch the single row for this guild, prefer most recent if multiple exist
    row, _ = await execute_query(
        """
        SELECT tokens_used, cost_usd, limit_usd, cycle_end
        FROM aimod_usage
        WHERE guild_id = %s
        ORDER BY cycle_end DESC
        LIMIT 1
        """,
        (guild_id,),
        fetch_one=True,
    )

    if not row:
        # Initialize with target_end
        await execute_query(
            """
            INSERT INTO aimod_usage (guild_id, cycle_end, tokens_used, cost_usd, limit_usd)
            VALUES (%s, %s, 0, 0, 2.00)
            """,
            (guild_id, target_end.replace(tzinfo=None)),
        )
        return {
            "tokens_used": 0,
            "cost_usd": 0.0,
            "limit_usd": DEFAULT_BUDGET_LIMIT_USD,
            "cycle_end": target_end,
        }

    tokens_used, cost_usd, limit_usd, stored_end = row
    if isinstance(stored_end, datetime) and stored_end.tzinfo is None:
        stored_end = stored_end.replace(tzinfo=timezone.utc)

    # If we've rolled into a new billing cycle, reset counters and update end
    if target_end > stored_end:
        await execute_query(
            """
            UPDATE aimod_usage
            SET tokens_used = 0,
                cost_usd = 0,
                cycle_end = %s
            WHERE guild_id = %s
            """,
            (target_end.replace(tzinfo=None), guild_id),
        )
        # Clean up any legacy duplicates
        await execute_query(
            "DELETE FROM aimod_usage WHERE guild_id = %s AND cycle_end <> %s",
            (guild_id, target_end.replace(tzinfo=None)),
        )
        return {
            "tokens_used": 0,
            "cost_usd": 0.0,
            "limit_usd": float(limit_usd or DEFAULT_BUDGET_LIMIT_USD),
            "cycle_end": target_end,
        }

    # Keep existing row; also dedupe any older duplicates
    await execute_query(
        "DELETE FROM aimod_usage WHERE guild_id = %s AND cycle_end <> %s",
        (guild_id, stored_end.replace(tzinfo=None)),
    )
    return {
        "tokens_used": int(tokens_used or 0),
        "cost_usd": float(cost_usd or 0),
        "limit_usd": float(limit_usd or DEFAULT_BUDGET_LIMIT_USD),
        "cycle_end": stored_end,
    }

async def add_aimod_usage(guild_id: int, tokens: int, cost_usd: float):
    """Increment usage counters for the current cycle for this guild.

    Single-row model: update by guild_id. If no row exists, initialize first.
    """
    snapshot = await get_aimod_usage(guild_id)
    # Ensure cost precision matches DECIMAL(12,6) to avoid MySQL truncation warnings
    try:
        cost_usd = round(float(cost_usd), 6)
        if not math.isfinite(cost_usd) or cost_usd < 0:
            cost_usd = 0.0
    except Exception:
        cost_usd = 0.0
    # Try update first
    _, affected = await execute_query(
        """
        UPDATE aimod_usage
        SET tokens_used = tokens_used + %s,
            cost_usd = cost_usd + %s
        WHERE guild_id = %s
        """,
        (int(tokens), cost_usd, guild_id),
    )
    if affected == 0:
        # Initialize row and retry
        await execute_query(
            """
            INSERT INTO aimod_usage (guild_id, cycle_end, tokens_used, cost_usd, limit_usd)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (guild_id, snapshot["cycle_end"].replace(tzinfo=None), int(tokens), cost_usd, DEFAULT_BUDGET_LIMIT_USD),
        )
    # Return updated snapshot
    return await get_aimod_usage(guild_id)

async def get_vcmod_usage(guild_id: int):
    """
    Get or initialize the current billing cycle usage for VC moderation.
    Mirrors aimod_usage but tracked separately.
    """
    target_end = await _get_current_cycle_end(guild_id)
    row, _ = await execute_query(
        """
        SELECT tokens_used, cost_usd, limit_usd, cycle_end
        FROM vcmod_usage
        WHERE guild_id = %s
        ORDER BY cycle_end DESC
        LIMIT 1
        """,
        (guild_id,),
        fetch_one=True,
    )

    if not row:
        await execute_query(
            """
            INSERT INTO vcmod_usage (guild_id, cycle_end, tokens_used, cost_usd, limit_usd)
            VALUES (%s, %s, 0, 0, 2.00)
            """,
            (guild_id, target_end.replace(tzinfo=None)),
        )
        return {
            "tokens_used": 0,
            "cost_usd": 0.0,
            "limit_usd": DEFAULT_BUDGET_LIMIT_USD,
            "cycle_end": target_end,
        }

    tokens_used, cost_usd, limit_usd, stored_end = row
    if isinstance(stored_end, datetime) and stored_end.tzinfo is None:
        stored_end = stored_end.replace(tzinfo=timezone.utc)

    if target_end > stored_end:
        await execute_query(
            """
            UPDATE vcmod_usage
            SET tokens_used = 0,
                cost_usd = 0,
                cycle_end = %s
            WHERE guild_id = %s
            """,
            (target_end.replace(tzinfo=None), guild_id),
        )
        await execute_query(
            "DELETE FROM vcmod_usage WHERE guild_id = %s AND cycle_end <> %s",
            (guild_id, target_end.replace(tzinfo=None)),
        )
        return {
            "tokens_used": 0,
            "cost_usd": 0.0,
            "limit_usd": float(limit_usd or DEFAULT_BUDGET_LIMIT_USD),
            "cycle_end": target_end,
        }

    await execute_query(
        "DELETE FROM vcmod_usage WHERE guild_id = %s AND cycle_end <> %s",
        (guild_id, stored_end.replace(tzinfo=None)),
    )
    return {
        "tokens_used": int(tokens_used or 0),
        "cost_usd": float(cost_usd or 0),
        "limit_usd": float(limit_usd or DEFAULT_BUDGET_LIMIT_USD),
        "cycle_end": stored_end,
    }

async def add_vcmod_usage(guild_id: int, tokens: int, cost_usd: float):
    snapshot = await get_vcmod_usage(guild_id)
    try:
        cost_usd = round(float(cost_usd), 6)
        if not math.isfinite(cost_usd) or cost_usd < 0:
            cost_usd = 0.0
    except Exception:
        cost_usd = 0.0

    _, affected = await execute_query(
        """
        UPDATE vcmod_usage
        SET tokens_used = tokens_used + %s,
            cost_usd = cost_usd + %s
        WHERE guild_id = %s
        """,
        (int(tokens), cost_usd, guild_id),
    )
    if affected == 0:
        await execute_query(
            """
            INSERT INTO vcmod_usage (guild_id, cycle_end, tokens_used, cost_usd, limit_usd)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (guild_id, snapshot["cycle_end"].replace(tzinfo=None), int(tokens), cost_usd, DEFAULT_BUDGET_LIMIT_USD),
        )
    return await get_vcmod_usage(guild_id)
