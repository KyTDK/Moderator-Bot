from __future__ import annotations

from typing import Any, Optional, Type

from modules.ai.mod_utils import (
    get_model_limit,
    pick_model,
    budget_allows,
    budget_allows_voice,
)
from modules.ai.costs import MAX_CONTEXT_USAGE_FRACTION
from modules.ai.engine import run_parsed_ai
from modules.utils import mysql

def build_user_prompt(rules: str, violation_history_blob: str, transcript: str) -> str:
    rules_prep = f"Rules:\n{rules}\n\n" if not rules.strip().lower().startswith("rules:") else f"{rules}\n\n"
    return f"{rules_prep}{violation_history_blob}Transcript:\n{transcript}"

def estimate_total_tokens(
    *,
    base_system_tokens: int,
    estimate_tokens_fn,
    parts: list[str],
) -> int:
    return base_system_tokens + sum(estimate_tokens_fn(p) for p in parts if p)

async def run_moderation_pipeline(
    *,
    guild_id: int,
    api_key: str,
    system_prompt: str,
    rules: str,
    violation_history_blob: str,
    transcript: str,
    base_system_tokens: int,
    default_model: str,
    high_accuracy: bool,
    text_format: Type[Any],
    estimate_tokens_fn,
    precomputed_total_tokens: Optional[int] = None,
) -> tuple[Optional[Any], int, float, dict, str]:
    """Unified moderation pipeline that handles token estimation, budget, AI call, and usage.

    Returns (report_or_none, total_tokens, request_cost, usage_snapshot).
    When budget is insufficient or context too large, report_or_none will be None.
    """
    model = pick_model(high_accuracy, default_model)
    # token estimate
    if precomputed_total_tokens is not None:
        total_tokens = int(precomputed_total_tokens)
    else:
        total_tokens = estimate_total_tokens(
            base_system_tokens=base_system_tokens,
            estimate_tokens_fn=estimate_tokens_fn,
            parts=[rules, violation_history_blob, transcript],
        )

    # Context window check
    limit = get_model_limit(model)
    max_tokens = int(limit * MAX_CONTEXT_USAGE_FRACTION)
    if total_tokens >= max_tokens:
        return None, total_tokens, 0.0, {}, "too_large"

    # Budget check
    allow, request_cost, usage = await budget_allows(guild_id, model, total_tokens)
    if not allow:
        return None, total_tokens, request_cost, usage, "budget"

    # Build prompt and call AI
    user_prompt = build_user_prompt(rules, violation_history_blob, transcript)
    report = await run_parsed_ai(
        api_key=api_key,
        model=model,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        text_format=text_format,
    )

    # Record usage on success
    await mysql.add_aimod_usage(guild_id, total_tokens, request_cost)
    return report, total_tokens, request_cost, usage, "ok"


async def run_moderation_pipeline_voice(
    *,
    guild_id: int,
    api_key: str,
    system_prompt: str,
    rules: str,
    transcript_only: bool,
    violation_history_blob: str,
    transcript: str,
    base_system_tokens: int,
    default_model: str,
    high_accuracy: bool,
    text_format: Type[Any],
    estimate_tokens_fn,
    precomputed_total_tokens: Optional[int] = None,
) -> tuple[Optional[Any], int, float, dict, str]:
    """Voice-specific pipeline variant using VC budget and usage accounting."""
    model = pick_model(high_accuracy, default_model)
    if precomputed_total_tokens is not None:
        total_tokens = int(precomputed_total_tokens)
    else:
        total_tokens = estimate_total_tokens(
            base_system_tokens=base_system_tokens,
            estimate_tokens_fn=estimate_tokens_fn,
            parts=[rules, violation_history_blob, transcript],
        )

    limit = get_model_limit(model)
    max_tokens = int(limit * MAX_CONTEXT_USAGE_FRACTION)
    if total_tokens >= max_tokens:
        return None, total_tokens, 0.0, {}, "too_large"

    allow, request_cost, usage = await budget_allows_voice(guild_id, model, total_tokens)
    if not allow:
        return None, total_tokens, request_cost, usage, "budget"
    
    if rules.strip() == "":
        # No rules means no context to guide moderation; skip to save cost
        return None, total_tokens, 0.0, usage, "no_rules"
    
    if transcript_only:
        # AI Moderation is disabled; skip to save cost
        return None, total_tokens, 0.0, usage, "transcript_only"
    
    user_prompt = build_user_prompt(rules, violation_history_blob, transcript)
    report = await run_parsed_ai(
        api_key=api_key,
        model=model,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        text_format=text_format,
    )

    await mysql.add_vcmod_usage(guild_id, total_tokens, request_cost)
    return report, total_tokens, request_cost, usage, "ok"
