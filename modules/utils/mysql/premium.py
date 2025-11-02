from datetime import datetime, timezone
from typing import Any, Dict, Optional

from modules.config.premium_plans import PLAN_CORE, PLAN_FREE, tier_to_plan

from .connection import execute_query

DEFAULT_PREMIUM_TIER = "accelerated"


def _parse_next_billing(value) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
            try:
                parsed = datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
                return parsed
            except ValueError:
                continue
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _resolve_activation(status: Optional[str], next_billing: Optional[datetime]) -> bool:
    now = datetime.now(timezone.utc)
    if status == "active" and (next_billing is None or next_billing > now):
        return True
    if status == "cancelled" and next_billing and next_billing > now:
        return True
    return False


async def is_accelerated(user_id: int | None = None, guild_id: int | None = None) -> bool:
    """Return True if the user or guild should have Accelerated access."""
    conditions: list[str] = []
    params: list[int] = []

    if guild_id is not None:
        conditions.append("guild_id = %s")
        params.append(guild_id)
    if user_id is not None:
        conditions.append("buyer_id = %s")
        params.append(user_id)

    if not conditions:
        return False

    where_clause = " AND ".join(conditions)
    query = f"""
        SELECT status, next_billing
        FROM premium_guilds
        WHERE {where_clause}
        LIMIT 1
    """

    row, _ = await execute_query(query, tuple(params), fetch_one=True)
    if not row:
        return False

    status = row[0]
    raw_next_billing = row[1] if len(row) > 1 else None
    next_billing = _parse_next_billing(raw_next_billing)
    return _resolve_activation(status, next_billing)


async def get_premium_status(guild_id: int) -> Optional[Dict[str, Any]]:
    """Return resolved premium metadata for a guild, or None if not found."""
    row, _ = await execute_query(
        "SELECT status, next_billing, tier FROM premium_guilds WHERE guild_id = %s LIMIT 1",
        (guild_id,),
        fetch_one=True,
    )
    if not row:
        return None

    status, next_billing_raw, tier = row[0], row[1], row[2] if len(row) > 2 else None
    normalized_tier = tier or DEFAULT_PREMIUM_TIER
    next_billing = _parse_next_billing(next_billing_raw)
    is_active = _resolve_activation(status, next_billing)
    
    return {
        "status": status,
        "next_billing": next_billing,
        "tier": normalized_tier,
        "is_active": is_active,
    }


async def resolve_guild_plan(guild_id: int) -> str:
    """Return the current active plan name for a guild (or free)."""
    status = await get_premium_status(guild_id)
    if not status or not status.get("is_active"):
        return PLAN_FREE
    tier = status.get("tier") or DEFAULT_PREMIUM_TIER
    plan = tier_to_plan(tier, default=PLAN_CORE)
    return plan or PLAN_CORE
