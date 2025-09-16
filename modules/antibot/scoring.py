from __future__ import annotations

import discord
from typing import Any, Dict, Tuple

from .utils import age_days


def evaluate_member(member: discord.Member) -> Tuple[int, Dict[str, Any]]:
    """
    Score a member with a simple, explainable heuristic.
    Returns (score [0..100], details dict).
    """
    try:
        user = member._user if hasattr(member, "_user") else member
    except Exception:
        user = member

    score = 0
    details: Dict[str, Any] = {}

    # 1) Account age
    created_days = age_days(getattr(user, "created_at", None))
    details["account_age_days"] = created_days
    if created_days is not None:
        if created_days >= 365:
            score += 25
        elif created_days >= 180:
            score += 20
        elif created_days >= 30:
            score += 10
        elif created_days <= 3:
            score -= 25
        elif created_days <= 7:
            score -= 15

    # 2) Guild join recency
    joined_days = age_days(getattr(member, "joined_at", None))
    details["guild_join_days"] = joined_days
    if joined_days is not None:
        if joined_days >= 60:
            score += 10
        elif joined_days <= 1:
            score -= 10

    # 3) Avatar/banner/accent signals
    has_avatar = bool(getattr(member, "guild_avatar", None) is not None or getattr(user, "avatar", None) is not None)
    details["has_avatar"] = has_avatar
    if has_avatar:
        score += 10
    else:
        score -= 10

    has_banner = getattr(user, "banner", None) is not None
    details["has_banner"] = has_banner
    if has_banner:
        score += 3

    has_accent = getattr(user, "accent_color", None) is not None
    details["has_accent_color"] = has_accent
    if has_accent:
        score += 2

    # 4) Roles (excluding @everyone)
    role_count = max(0, len([r for r in member.roles if r != member.guild.default_role]))
    details["role_count"] = role_count
    if role_count >= 5:
        score += 15
    elif role_count >= 2:
        score += 10
    elif role_count == 0:
        score -= 10

    # 5) Presence & activities
    status = getattr(member, "status", discord.Status.offline)
    acts = list(getattr(member, "activities", []) or [])
    has_activity = len(acts) > 0
    details["status"] = str(status)
    details["activities_count"] = len(acts)
    if status != discord.Status.offline:
        score += 5
    if has_activity:
        score += 10
        if any(getattr(a, "type", None) == discord.ActivityType.playing for a in acts):
            score += 3
        if any(getattr(a, "type", None) == discord.ActivityType.listening for a in acts):
            score += 2

    # 6) Membership screening pending
    pending = getattr(member, "pending", False)
    details["membership_screening_pending"] = pending
    if pending:
        score -= 25

    # 7) Public flags (hypesquad etc.)
    flags = []
    try:
        pf = getattr(user, "public_flags", None)
        flags = list(pf.all()) if pf else []
    except Exception:
        flags = []
    details["public_flags"] = [str(f) for f in flags]
    if flags:
        score += 5

    # 8) Bot account?
    is_bot = bool(getattr(user, "bot", False))
    details["is_bot_account"] = is_bot
    if is_bot:
        score = min(score, 0)

    # Clamp & return
    score = max(0, min(100, score))
    details["final_score"] = score
    return score, details

