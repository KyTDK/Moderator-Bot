from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import discord
from discord import Color, Embed, Interaction, Member
from discord.ext import commands

from modules.i18n import get_translated_mapping
from modules.utils import mod_logging, mysql
from modules.utils.discord_utils import message_user
from modules.utils.mysql import execute_query
from modules.utils.time import parse_duration

from .actions import perform_disciplinary_action
from .texts import (
    STRIKE_ERRORS_FALLBACK,
    STRIKE_TEXTS_FALLBACK,
)


def get_ban_threshold(strike_settings):
    """
    Given a settings dict mapping strike numbers to an action and duration,
    returns the strike count when a "ban" is applied (e.g., 'Ban') or None if no ban is set.
    """
    available_strikes = sorted(strike_settings.keys(), key=int)
    for strike in available_strikes:
        entry = strike_settings[strike]
        if isinstance(entry, tuple):
            action = entry[0]
        elif isinstance(entry, list):
            if not entry:
                continue
            action = entry[0].split(":", 1)[0]
        else:
            action = str(entry).split(":", 1)[0]
        if action.lower() == "ban":
            return int(strike)
    return None


async def strike(
    user: Member,
    bot: commands.Bot,
    reason: str = "No reason provided",
    interaction: Optional[Interaction] = None,
    expiry: Optional[str] = None,
    skip_punishments: bool = False,
    log_to_channel: bool = True,
) -> Optional[Embed]:
    if interaction:
        await interaction.response.defer(ephemeral=True)
        strike_by = interaction.user
    else:
        strike_by = bot.user

    strike_texts = get_translated_mapping(
        bot,
        "modules.moderation.strike.strike",
        STRIKE_TEXTS_FALLBACK,
        guild_id=user.guild.id,
    )
    errors_texts = get_translated_mapping(
        bot,
        "modules.moderation.strike.errors",
        STRIKE_ERRORS_FALLBACK,
        guild_id=user.guild.id,
    )

    guild_id = user.guild.id
    if not expiry:
        expiry = await mysql.get_settings(guild_id, "strike-expiry")

    default_reason = strike_texts.get("default_reason", STRIKE_TEXTS_FALLBACK["default_reason"])
    reason = reason or default_reason

    now = datetime.now(timezone.utc)
    expires_at = None
    if expiry:
        delta = parse_duration(str(expiry))
        if delta:
            expires_at = now + delta

    query = """
        INSERT INTO strikes (guild_id, user_id, reason, striked_by_id, timestamp, expires_at)
        VALUES (%s, %s, %s, %s, %s, %s)
    """
    await execute_query(
        query,
        (
            guild_id,
            user.id,
            reason,
            strike_by.id,
            now,
            expires_at,
        ),
    )

    strike_count = await mysql.get_strike_count(user.id, guild_id)
    if interaction and strike_count > 100:
        await interaction.followup.send(
            errors_texts["too_many_strikes"],
            ephemeral=True,
        )
        return None

    strike_settings = await mysql.get_settings(guild_id, "strike-actions")
    cycle_settings = await mysql.get_settings(guild_id, "cycle-strike-actions")
    available_strikes = sorted(strike_settings.keys(), key=int)

    actions = strike_settings.get(str(strike_count), [])

    if not actions and cycle_settings:
        available_strike_values = [strike_settings[k] for k in available_strikes]
        index = (strike_count - 1) % len(available_strike_values)
        actions = available_strike_values[index]

    strikes_for_ban = get_ban_threshold(strike_settings)
    strikes_till_ban = strikes_for_ban - strike_count if strikes_for_ban is not None else None

    configured_actions = actions
    action_description = "\n" + strike_texts["action_none"]

    if skip_punishments and configured_actions:
        action_description = "\n" + strike_texts.get(
            "action_skipped",
            STRIKE_TEXTS_FALLBACK["action_skipped"],
        )

    if configured_actions and not skip_punishments:
        try:
            action_result_text = await perform_disciplinary_action(
                user=user,
                bot=bot,
                action_string=configured_actions,
                reason=reason,
            )
        except Exception as exc:  # pragma: no cover - defensive logging
            print(
                f"[Strike] Failed to apply disciplinary actions for guild {guild_id} user {user.id}: {exc}"
            )
            action_result_text = None

        if action_result_text:
            action_lines = [
                line.strip()
                for line in action_result_text.splitlines()
                if line.strip()
            ]
            if action_lines:
                action_description = (
                    "\n"
                    + strike_texts["actions_heading"]
                    + "\n"
                    + "\n".join(
                        strike_texts["action_item"].format(action=line)
                        for line in action_lines
                    )
                )
        else:
            action_description = "\n" + strike_texts["action_none"]

    strike_info = "\n" + strike_texts["strike_count"].format(count=strike_count)
    if strikes_till_ban is not None and strikes_till_ban > 0:
        strike_info += " " + strike_texts["strike_until_ban"].format(
            remaining=strikes_till_ban
        )

    expiry_str = (
        f"<t:{int(expires_at.timestamp())}:R>" if expires_at else strike_texts["expiry_never"]
    )
    embed = Embed(
        title=strike_texts["embed_title_user"],
        description=(
            strike_texts["reason"].format(reason=reason)
            + action_description
            + strike_info
            + "\n"
            + strike_texts["expires"].format(expiry=expiry_str)
        ),
        color=Color.red(),
        timestamp=now,
    )

    embed.add_field(
        name=strike_texts["issued_by"],
        value=f"{strike_by.mention} ({strike_by})",
        inline=False,
    )
    embed.set_footer(
        text=strike_texts["footer"].format(server=user.guild.name),
        icon_url=user.guild.icon.url if user.guild.icon else None,
    )

    if await mysql.get_settings(user.guild.id, "dm-on-strike"):
        try:
            await message_user(user, "", embed=embed)
        except Exception:
            if interaction:
                await interaction.channel.send(user.mention, embed=embed)
            return embed

    embed.title = strike_texts["embed_title_public"].format(name=user.display_name)
    strikes_channel_id = await mysql.get_settings(user.guild.id, "strike-channel")
    if strikes_channel_id is not None and log_to_channel:
        await mod_logging.log_to_channel(embed, strikes_channel_id, bot)

    return embed


__all__ = ["strike", "get_ban_threshold"]
