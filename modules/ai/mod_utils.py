from __future__ import annotations

from typing import Tuple

from modules.utils import mysql


PRICES_PER_MTOK = {
    "gpt-5-nano": 0.45,
    "gpt-5-mini": 2.25,
}


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


def get_model_limit(model_name: str) -> int:
    return next((limit for key, limit in MODEL_CONTEXT_WINDOWS.items() if key in model_name), 16000)


def pick_model(high_accuracy: bool, default_model: str) -> str:
    return "gpt-5-mini" if high_accuracy else default_model


async def budget_allows(
    guild_id: int,
    model_name: str,
    estimated_tokens: int,
) -> Tuple[bool, float, dict]:
    """Check if the estimated request fits within the current budget.

    Returns (allow, request_cost, usage_snapshot_dict).
    """
    usage = await mysql.get_aimod_usage(guild_id)
    price_per_token = get_price_per_mtok(model_name) / 1_000_000
    request_cost = round(estimated_tokens * price_per_token, 6)
    allow = (usage.get("cost_usd", 0.0) + request_cost) <= usage.get("limit_usd", 2.0)
    return allow, request_cost, usage

