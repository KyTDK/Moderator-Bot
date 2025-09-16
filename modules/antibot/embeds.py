from __future__ import annotations

import discord
from typing import Any, Dict, List

from .utils import fmt_bool, age_compact


def _color_for_score(score: int) -> discord.Color:
    return (
        discord.Color.green() if score >= 70 else (
            discord.Color.orange() if score >= 40 else discord.Color.red()
        )
    )


def build_inspection_embed(
    member: discord.Member,
    score: int,
    details: Dict[str, Any],
) -> discord.Embed:
    emb = discord.Embed(
        title=f"User Inspection: {member}",
        color=_color_for_score(score),
    )
    emb.set_thumbnail(url=member.display_avatar.url)

    banner_url = details.get('banner_url')
    if banner_url:
        emb.set_image(url=str(banner_url))

    contrib = (details or {}).get("contrib") or {}

    emb.add_field(
        name="Overview",
        value=(
            f"ID: `{member.id}`\n"
            f"Bot: `{fmt_bool(getattr(member, 'bot', False))}`\n"
            f"Status: `{details.get('status')}`\n"
            f"Activities: `{details.get('activities_count')}`\n"
        ),
        inline=False,
    )

    accent_value = details.get('accent_color_value')
    accent_label = fmt_bool(details.get('has_accent_color', False))
    if isinstance(accent_value, int):
        accent_label = f"{accent_label} (#{accent_value:06X})"

    banner_label = fmt_bool(details.get('has_banner', False))

    has_decoration = bool(getattr(member, 'avatar_decoration', None) or getattr(member, 'avatar_decoration_data', None))
    emb.add_field(
        name="Account",
        value=(
            f"Created: `{member.created_at}` (≈ {age_compact(member.created_at)})\n"
            f"Avatar: `{fmt_bool(details.get('has_avatar', False))}` | "
            f"Banner: `{banner_label}` | "
            f"Accent: `{accent_label}` | "
            f"Decoration: `{fmt_bool(has_decoration)}`\n"
        ),
        inline=False,
    )

    public_flags_str = (details.get('public_flags') or 'none')
    extra_badges_raw = list(details.get('badges_extra') or [])
    badge_lines = [f"Public Flags: `{public_flags_str}`"]
    if extra_badges_raw:
        badge_lines.append(f"Extra Badges: `{', '.join(map(str, extra_badges_raw[:10])) or 'none'}`")
    badge_weight = contrib.get('public_flags_weight')
    if badge_weight:
        badge_lines.append(f"Public Flag Weight: {badge_weight:+d}")
    extra_weight = contrib.get('extra_badges')
    if extra_weight:
        badge_lines.append(f"Extra Badge Weight: {extra_weight:+d}")

    emb.add_field(name="Badges", value="\n".join(badge_lines), inline=False)

    identity_lines = []
    primary_guild = details.get('primary_guild')
    if primary_guild:
        identity_lines.append(f"Server Tag: `{primary_guild}`")
    collectibles = list(details.get('collectibles') or [])
    collectibles_total = details.get('collectibles_count') or len(collectibles)
    if collectibles:
        preview = ', '.join(collectibles[:5]) or 'none'
        if collectibles_total > 5 and len(collectibles) >= 5:
            preview = f"{preview} (+{collectibles_total - 5} more)"
        identity_lines.append(f"Collectibles ({collectibles_total}): `{preview}`")
    elif primary_guild:
        identity_lines.append("Collectibles (0): `none`")
    member_flags = list(details.get('member_flags_list') or [])
    member_flags_total = details.get('member_flags_count') or len(member_flags)
    if member_flags:
        flags_preview = ', '.join(member_flags[:5]) or 'none'
        if member_flags_total > 5 and len(member_flags) >= 5:
            flags_preview = f"{flags_preview} (+{member_flags_total - 5} more)"
        identity_lines.append(f"Member Flags ({member_flags_total}): `{flags_preview}`")
    elif member_flags_total == 0 and identity_lines:
        identity_lines.append("Member Flags (0): `none`")
    if identity_lines:
        emb.add_field(name="Identity", value='\n'.join(identity_lines), inline=False)


    roles = [r.mention for r in member.roles if r != member.guild.default_role]
    role_str = ", ".join(roles[:10]) if roles else "none"
    emb.add_field(
        name="Guild",
        value=(
            f"Joined: `{member.joined_at}` (≈ {age_compact(member.joined_at)})\n"
            f"Roles ({len(roles)}): {role_str}\n"
            f"Screening Pending: `{fmt_bool(details.get('membership_screening_pending', False))}`\n"
            f"Boosting: `{fmt_bool(member.premium_since is not None)}`"
        ),
        inline=False,
    )

    acts: List[discord.Activity] = getattr(member, "activities", []) or []
    if acts:
        lines = []
        for a in acts[:5]:
            aname = getattr(a, "name", None) or getattr(a, "state", None) or str(a)
            atype = getattr(a, "type", None)
            lines.append(f"- {atype.name if hasattr(atype, 'name') else atype}: {aname}")
        emb.add_field(name="Recent Activity", value="\n".join(lines), inline=False)

    emb.add_field(name="Trust Score", value=f"`{score}` / 100", inline=False)

    # Weighted signals (top 10 by magnitude)
    if contrib:
        pairs = sorted(contrib.items(), key=lambda kv: abs(kv[1]), reverse=True)[:10]
        lines = [f"{k}: {'+' if v>=0 else ''}{v}" for k, v in pairs]
        emb.add_field(name="Signals (weighted)", value="\n".join(lines), inline=False)

    emb.set_footer(text="Note: Discord profile connections are not available to bots.")
    return emb


def build_join_embed(
    member: discord.Member,
    score: int,
    details: Dict[str, Any],
) -> discord.Embed:
    emb = discord.Embed(
        title="Anti-Bot Check: Member Joined",
        description=f"{member.mention} (`{member.id}`)\nScore: `{score}` / 100",
        color=_color_for_score(score),
    )
    emb.set_thumbnail(url=member.display_avatar.url)
    emb.add_field(
        name="Signals",
        value=(
            f"Account age: `{details.get('account_age_days')}`d | "
            f"Joined: `{details.get('guild_join_days')}`d\n"
            f"Avatar: `{fmt_bool(details.get('has_avatar', False))}` | "
            f"Pending: `{fmt_bool(details.get('membership_screening_pending', False))}`\n"
            f"Status: `{details.get('status')}` | "
            f"Activities: `{details.get('activities_count')}`\n"
            f"Server Tag: `{details.get('primary_guild') or 'none'}` | "
            f"Collectibles: `{details.get('collectibles_count') or 0}` | "
            f"Member Flags: `{details.get('member_flags_count') or 0}`"
        ),
        inline=False,
    )

    return emb
