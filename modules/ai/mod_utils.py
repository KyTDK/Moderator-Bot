from __future__ import annotations

from typing import Tuple

from modules.utils import mysql
from modules.ai.costs import (
    PRICES_PER_MTOK,
    ACCELERATED_BUDGET_LIMIT_USD,
    ACCELERATED_PRO_BUDGET_LIMIT_USD,
    ACCELERATED_ULTRA_BUDGET_LIMIT_USD,
)


def get_price_per_mtok(model_name: str) -> float:
    return next((v for k, v in PRICES_PER_MTOK.items() if k in model_name), 0.45)


MODEL_CONTEXT_WINDOWS = {
    "gpt-5-nano": 128000,
    "gpt-5-mini": 128000,
    "gpt-5": 128000,
    "gpt-4.1": 1000000,
    "gpt-4.1-nano": 1000000,
    "gpt-4.1-mini": 1000000,
    "gpt-4o": 128000,
    "gpt-4o-mini": 128000,
}


TIER_BUDGET_LIMITS = {
    "accelerated": ACCELERATED_BUDGET_LIMIT_USD,
    "accelerated_pro": ACCELERATED_PRO_BUDGET_LIMIT_USD,
    "accelerated_ultra": ACCELERATED_ULTRA_BUDGET_LIMIT_USD,
}

BUDGET_EPSILON = 1e-6


def get_model_limit(model_name: str) -> int:
    return next((limit for key, limit in MODEL_CONTEXT_WINDOWS.items() if key in model_name), 16000)


def pick_model(high_accuracy: bool, default_model: str) -> str:
    return "gpt-5-mini" if high_accuracy else default_model


async def _resolve_budget_limit(guild_id: int, usage: dict, table: str) -> float:
    try:
        stored_limit = float(usage.get("limit_usd", ACCELERATED_BUDGET_LIMIT_USD))
    except (TypeError, ValueError):
        stored_limit = ACCELERATED_BUDGET_LIMIT_USD

    if stored_limit <= 0:
        stored_limit = ACCELERATED_BUDGET_LIMIT_USD

    premium = await mysql.get_premium_status(guild_id)
    if premium and premium.get("is_active"):
        tier = premium.get("tier")
        tier_limit = TIER_BUDGET_LIMITS.get(tier, ACCELERATED_BUDGET_LIMIT_USD)
    else:
        tier_limit = ACCELERATED_BUDGET_LIMIT_USD

    effective_limit = max(stored_limit, tier_limit)

    if abs(stored_limit - effective_limit) > BUDGET_EPSILON:
        usage["limit_usd"] = effective_limit
        await mysql.execute_query(
            f"UPDATE {table} SET limit_usd = %s WHERE guild_id = %s",
            (effective_limit, guild_id),
        )
    else:
        usage["limit_usd"] = stored_limit

    return effective_limit


async def budget_allows(
    guild_id: int,
    model_name: str,
    estimated_tokens: int,
) -> Tuple[bool, float, dict]:
    """Check if the estimated request fits within the current budget.

    Returns (allow, request_cost, usage_snapshot_dict).
    """
    usage = await mysql.get_aimod_usage(guild_id)
    limit = await _resolve_budget_limit(guild_id, usage, "aimod_usage")
    price_per_token = get_price_per_mtok(model_name) / 1_000_000
    request_cost = round(estimated_tokens * price_per_token, 6)
    allow = (usage.get("cost_usd", 0.0) + request_cost) <= limit
    return allow, request_cost, usage


async def budget_allows_voice(
    guild_id: int,
    model_name: str,
    estimated_tokens: int,
) -> Tuple[bool, float, dict]:
    """Voice-specific budget check using vcmod_usage only (no settings override).

    Returns (allow, request_cost, usage_snapshot_dict).
    """
    usage = await mysql.get_vcmod_usage(guild_id)
    limit = await _resolve_budget_limit(guild_id, usage, "vcmod_usage")
    price_per_token = get_price_per_mtok(model_name) / 1_000_000
    request_cost = round(estimated_tokens * price_per_token, 6)
    allow = (usage.get("cost_usd", 0.0) + request_cost) <= limit
    return allow, request_cost, usage
