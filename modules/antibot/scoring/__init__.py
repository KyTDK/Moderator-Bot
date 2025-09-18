from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

import discord

from .config import MAX_SCORE, MIN_SCORE
from .context import ScoreContext, resolve_user
from . import identity, presence, profile, reputation, timeline

__all__ = ["evaluate_member"]

_MODULE_PIPELINE = (
    timeline,
    profile,
    presence,
    reputation,
    identity,
)

def evaluate_member(
    member: discord.Member,
    bot: Optional[discord.Client] = None,
) -> Tuple[int, Dict[str, Any]]:
    """Score a member with explainable heuristics.

    Returns "(score 0..100, details dict)".
    """
    user = resolve_user(member)
    ctx = ScoreContext(member=member, user=user, bot=bot)

    for module in _MODULE_PIPELINE:
        module.apply(ctx)

    ctx.clamp(MIN_SCORE, MAX_SCORE)
    details = ctx.result_details()
    return ctx.score, details
