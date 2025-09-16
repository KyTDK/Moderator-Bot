from __future__ import annotations

import discord
from typing import Any, Dict, Tuple, Optional

from .utils import age_days, shannon_entropy, digits_ratio, longest_digit_run


def evaluate_member(member: discord.Member, bot: Optional[discord.Client] = None) -> Tuple[int, Dict[str, Any]]:
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
    contrib: Dict[str, int] = {}

    # 1) Account age
    created_days = age_days(getattr(user, "created_at", None))
    details["account_age_days"] = created_days
    if created_days is not None:
        if created_days >= 365:
            score += 25; contrib["account_age>=365d"] = 25
        elif created_days >= 180:
            score += 20; contrib["account_age>=180d"] = 20
        elif created_days >= 30:
            score += 10; contrib["account_age>=30d"] = 10
        elif created_days <= 3:
            score -= 25; contrib["account_age<=3d"] = -25
        elif created_days <= 7:
            score -= 15; contrib["account_age<=7d"] = -15

    # 2) Guild join recency
    joined_days = age_days(getattr(member, "joined_at", None))
    details["guild_join_days"] = joined_days
    if joined_days is not None:
        if joined_days >= 60:
            score += 10; contrib["guild_tenure>=60d"] = 10
        elif joined_days <= 1:
            score -= 10; contrib["joined<=1d"] = -10

    # 3) Avatar/banner/accent signals
    try:
        is_default_avatar = member.display_avatar.is_default()
    except Exception:
        is_default_avatar = not bool(getattr(user, "avatar", None))
    has_avatar = not is_default_avatar
    details["has_avatar"] = has_avatar
    details["default_avatar"] = is_default_avatar
    if has_avatar:
        score += 12; contrib["avatar_present"] = 12
    else:
        score -= 12; contrib["avatar_missing"] = -12

    # Server-specific avatar is a stronger social signal
    if getattr(member, "guild_avatar", None) is not None:
        score += 2; contrib["server_avatar"] = contrib.get("server_avatar", 0) + 2

    has_banner = getattr(user, "banner", None) is not None
    details["has_banner"] = has_banner
    if has_banner:
        score += 3; contrib["banner_present"] = 3

    has_accent = getattr(user, "accent_color", None) is not None
    details["has_accent_color"] = has_accent
    if has_accent:
        score += 2; contrib["accent_color"] = 2

    # 4) Roles (excluding @everyone)
    role_count = max(0, len([r for r in member.roles if r != member.guild.default_role]))
    details["role_count"] = role_count
    # Roles are a weak signal and can be gamed
    if role_count >= 5:
        score += 8; contrib["roles>=5"] = 8
    elif role_count >= 2:
        score += 5; contrib["roles>=2"] = 5
    elif role_count == 0:
        score -= 8; contrib["roles=0"] = -8

    # 5) Presence & activities
    status = getattr(member, "status", discord.Status.offline)
    acts = list(getattr(member, "activities", []) or [])
    has_activity = len(acts) > 0
    details["status"] = str(status)
    details["activities_count"] = len(acts)
    if status != discord.Status.offline:
        score += 5; contrib["status!=offline"] = 5
    if has_activity:
        score += 10; contrib["has_activity"] = 10
        if any(getattr(a, "type", None) == discord.ActivityType.playing for a in acts):
            score += 3; contrib["playing"] = contrib.get("playing", 0) + 3
        if any(getattr(a, "type", None) == discord.ActivityType.listening for a in acts):
            score += 2; contrib["listening"] = contrib.get("listening", 0) + 2
        if any(getattr(a, "type", None) == discord.ActivityType.streaming for a in acts):
            score += 4; contrib["streaming"] = contrib.get("streaming", 0) + 4

    # 6) Membership screening pending
    pending = getattr(member, "pending", False)
    details["membership_screening_pending"] = pending
    if pending:
        score -= 25; contrib["membership_screening_pending"] = -25

    # 7) Public flags (hypesquad etc.)
    flags = []
    try:
        pf = getattr(user, "public_flags", None)
        flags = list(pf.all()) if pf else []
    except Exception:
        flags = []
    details["public_flags"] = [str(f) for f in flags]
    if flags:
        score += 5; contrib["public_flags"] = 5

    # 8.5) Nitro boosting is a strong human signal
    try:
        if getattr(member, "premium_since", None):
            score += 5; contrib["boosting"] = 5
    except Exception:
        pass

    # 8) Bot account?
    is_bot = bool(getattr(user, "bot", False))
    details["is_bot_account"] = is_bot
    if is_bot:
        contrib["bot_account"] = min(0, -score) if score > 0 else 0
        score = min(score, 0)

    # 9) Username heuristics
    try:
        uname = (getattr(user, "global_name", None) or getattr(user, "name", "") or "").strip()
    except Exception:
        uname = ""
    uname_l = uname.lower()
    ent = shannon_entropy(uname_l)
    dr = digits_ratio(uname_l)
    ldr = longest_digit_run(uname_l)
    details["name_entropy"] = round(ent, 2)
    details["name_digits_ratio"] = round(dr, 2)
    details["name_longest_digit_run"] = ldr
    suspicious_kw = any(k in uname_l for k in ("bot", "spam", "giveaway", "airdrop"))
    details["name_suspicious_kw"] = suspicious_kw
    # Apply cautious weights
    if suspicious_kw and not is_bot:
        score -= 5; contrib["name_keyword"] = -5
    if dr >= 0.5 and ldr >= 5 and len(uname_l) >= 8:
        score -= 10; contrib["many_digits"] = -10
    if ent <= 2.2 and len(uname_l) >= 6:
        score -= 5; contrib["low_entropy_name"] = -5

    # 10) Creation -> Join interval
    try:
        if getattr(user, "created_at", None) and getattr(member, "joined_at", None):
            delta_min = int((member.joined_at - user.created_at).total_seconds() // 60)
            details["creation_to_join_minutes"] = max(delta_min, 0)
            if delta_min <= 10:
                score -= 15; contrib["join_soon_after_creation"] = -15
            elif delta_min <= 60:
                score -= 10; contrib["join_within_1h"] = -10
            elif delta_min <= 1440:  # 1 day
                score -= 5; contrib["join_within_1d"] = -5
    except Exception:
        pass

    # 11) Mutual guilds with this bot (cache-only)
    try:
        if bot is not None:
            mg = 0
            for g in getattr(bot, "guilds", []) or []:
                if g.id == member.guild.id:
                    continue
                if g.get_member(member.id) is not None:
                    mg += 1
                if mg >= 5:
                    break
            details["mutual_guilds_with_bot"] = mg
            if mg >= 3:
                score += 5; contrib["mutual_guilds>=3"] = 5
            elif mg >= 2:
                score += 3; contrib["mutual_guilds>=2"] = 3
    except Exception:
        pass

    # Clamp & return
    score = max(0, min(100, score))
    details["final_score"] = score
    details["contrib"] = contrib
    return score, details
