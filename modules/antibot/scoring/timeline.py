from __future__ import annotations

from .config import (
    ACCOUNT_AGE_BONUSES,
    ACCOUNT_AGE_PENALTIES,
    CREATION_TO_JOIN_BONUSES,
    CREATION_TO_JOIN_PENALTIES,
    GUILD_TENURE_BONUSES,
    GUILD_TENURE_PENALTIES,
    MEMBERSHIP_PENDING_PENALTY,
)
from .context import ScoreContext
from ..utils import age_days

__all__ = ["apply"]

def apply(ctx: ScoreContext) -> None:
    """Apply all timeline related scoring rules."""
    _score_account_age(ctx)
    _score_guild_tenure(ctx)
    _score_membership_pending(ctx)
    _score_creation_to_join_delta(ctx)

def _score_account_age(ctx: ScoreContext) -> None:
    created_at = getattr(ctx.user, "created_at", None)
    created_days = age_days(created_at)
    ctx.set_detail("account_age_days", created_days)
    if created_days is None:
        return

    for rule in ACCOUNT_AGE_BONUSES:
        if created_days >= rule["min_days"]:
            ctx.add(rule["label"], rule["score"])
            break

    for rule in ACCOUNT_AGE_PENALTIES:
        if created_days <= rule["max_days"]:
            ctx.add(rule["label"], rule["score"])
            break

def _score_guild_tenure(ctx: ScoreContext) -> None:
    joined_at = getattr(ctx.member, "joined_at", None)
    joined_days = age_days(joined_at)
    ctx.set_detail("guild_join_days", joined_days)
    if joined_days is None:
        return

    for rule in GUILD_TENURE_BONUSES:
        if joined_days >= rule["min_days"]:
            ctx.add(rule["label"], rule["score"])
            break

    for rule in GUILD_TENURE_PENALTIES:
        if joined_days <= rule["max_days"]:
            ctx.add(rule["label"], rule["score"])
            break

def _score_membership_pending(ctx: ScoreContext) -> None:
    pending = getattr(ctx.member, "pending", False)
    ctx.set_detail("membership_screening_pending", pending)
    if pending:
        rule = MEMBERSHIP_PENDING_PENALTY
        ctx.add(rule["label"], rule["score"])

def _score_creation_to_join_delta(ctx: ScoreContext) -> None:
    user_created_at = getattr(ctx.user, "created_at", None)
    member_joined_at = getattr(ctx.member, "joined_at", None)
    if not user_created_at or not member_joined_at:
        return

    try:
        delta_minutes = int((member_joined_at - user_created_at).total_seconds() // 60)
    except Exception:
        return

    ctx.set_detail("creation_to_join_minutes", max(delta_minutes, 0))

    for rule in CREATION_TO_JOIN_PENALTIES:
        if delta_minutes <= rule["max_minutes"]:
            ctx.add(rule["label"], rule["score"])
            break

    for rule in CREATION_TO_JOIN_BONUSES:
        if delta_minutes >= rule["min_minutes"]:
            ctx.add(rule["label"], rule["score"])
            break
