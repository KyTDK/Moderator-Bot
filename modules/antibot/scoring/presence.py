from __future__ import annotations

import discord

from .config import (
    ACTIVITY_BASE_BONUS,
    ACTIVITY_MANY_BONUS,
    ACTIVITY_TYPE_WEIGHTS,
    PLATFORM_PRESENCE_BONUS,
    STATUS_BONUS,
)
from .context import ScoreContext

__all__ = ["apply"]

def apply(ctx: ScoreContext) -> None:
    """Apply presence and activity related rules."""
    _score_status(ctx)
    _score_activities(ctx)
    _score_platform_presence(ctx)

def _score_status(ctx: ScoreContext) -> None:
    status = getattr(ctx.member, "status", discord.Status.offline)
    ctx.set_detail("status", str(status))
    if status != discord.Status.offline:
        ctx.add(STATUS_BONUS["label"], STATUS_BONUS["score"])

def _score_activities(ctx: ScoreContext) -> None:
    activities = list(getattr(ctx.member, "activities", []) or [])
    ctx.set_detail("activities_count", len(activities))
    if not activities:
        return

    ctx.add(ACTIVITY_BASE_BONUS["label"], ACTIVITY_BASE_BONUS["score"])
    for activity in activities:
        activity_type = getattr(activity, "type", None)
        rule = ACTIVITY_TYPE_WEIGHTS.get(activity_type)
        if rule:
            ctx.add(rule["label"], rule["score"])

    if len(activities) >= ACTIVITY_MANY_BONUS["min_count"]:
        ctx.add(ACTIVITY_MANY_BONUS["label"], ACTIVITY_MANY_BONUS["score"])

def _score_platform_presence(ctx: ScoreContext) -> None:
    try:
        statuses = [
            getattr(ctx.member, "desktop_status", None),
            getattr(ctx.member, "web_status", None),
            getattr(ctx.member, "mobile_status", None),
        ]
        online_platforms = sum(1 for s in statuses if s and s != discord.Status.offline)
    except Exception:
        online_platforms = 0

    ctx.set_detail("platforms_online", online_platforms)
    if online_platforms >= PLATFORM_PRESENCE_BONUS["min_platforms"]:
        ctx.add(PLATFORM_PRESENCE_BONUS["label"], PLATFORM_PRESENCE_BONUS["score"])
