from typing import Optional
from discord import Interaction, Member, Embed, Color
from discord.ext import commands
from modules.utils.user_utils import message_user
from modules.utils.mysql import execute_query
from datetime import datetime, timedelta, timezone
from modules.utils import logging
from modules.utils import mysql
from modules.utils.time import parse_duration
import discord

def get_ban_threshold(strike_settings):
    """
    Given a settings dict mapping strike numbers to an action and duration,
    returns the strike count when a "ban" is applied (e.g., 'Ban') or None if no ban is set.
    """
    # Get the available strike thresholds as integers
    available_strikes = sorted(strike_settings.keys(), key=int)
    
    # Iterate over each strike threshold in ascending order
    for strike in available_strikes:
        action, duration_str = strike_settings[strike]
        if action.lower() == "ban":
            return int(strike)
    return None

async def perform_disciplinary_action(
    user: Member,
    bot: commands.Bot,
    action_string: str,
    reason: str = "NSFW profile picture detected"
) -> Optional[str]:
    """Executes a configured action string on a user."""
    now = datetime.now(timezone.utc)

    try:
        if action_string == "none":
            return "No action taken."

        elif action_string == "strike":
            await strike(user=user, bot=bot, reason=reason, expiry=duration_str)
            return "Strike issued."

        elif action_string == "kick":
            await user.kick(reason=reason)
            return "User kicked."

        elif action_string == "ban":
            await user.ban(reason=reason)
            return "User banned."

        elif action_string.startswith("timeout:"):
            duration_str = action_string.split("timeout:", 1)[1]
            delta = parse_duration(duration_str)
            if not delta:
                return f"Invalid timeout duration: '{duration_str}'"
            until = now + delta
            await user.timeout(until, reason=reason)
            return f"User timed out until <t:{int(until.timestamp())}:R>."

        return f"Unknown action: '{action_string}'"

    except Exception as e:
        print(f"[NSFW PFP Action Error] {user}: {e}")
        return f"Action failed: {action_string}"

async def strike(
    user: Member,
    bot: commands.Bot,
    reason: str = "No reason provided",
    interaction: Optional[Interaction] = None,
    expiry: Optional[str] = None,
    log_to_channel: bool = True
) -> discord.Embed:
    if interaction:
        await interaction.response.defer(ephemeral=True)
        strike_by = interaction.user
    else:
        strike_by = bot.user

    guild_id = user.guild.id
    if not expiry:
        expiry = await mysql.get_settings(guild_id, "strike-expiry")

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
    await execute_query(query, (
        guild_id,
        user.id,
        reason,
        strike_by.id,
        now,
        expires_at
    ))

    strike_count = await mysql.get_strike_count(user.id, guild_id)
    if interaction and strike_count > 100:
        await interaction.followup.send("You cannot give the same player more than 100 strikes. Use `strikes clear <user>` to reset their strikes.")
        return None

    strike_settings = await mysql.get_settings(guild_id, "strike-actions")
    available_strikes = sorted(strike_settings.keys(), key=int)
    action, duration_str = strike_settings.get(str(strike_count), (None, None))

    strikes_for_ban = get_ban_threshold(strike_settings)
    strikes_till_ban = strikes_for_ban - strike_count if strikes_for_ban is not None else None

    duration = parse_duration(duration_str)
    if action is not None:
        action = action.lower()

    if action == "timeout":
        if duration is None:
            duration = timedelta(days=1)
        until = now + duration
        action_description = f"\n**Action Taken:** Timeout, will expire <t:{int(until.timestamp())}:R>"
    elif action == "ban":
        action_description = "\n**Action Taken:** Banned from the server"
    elif action == "kick":
        action_description = "\n**Action Taken:** Kicked from the server"
    else:
        action_description = "\n**Action Taken:** No action applied"

    if strike_count < len(available_strikes):
        strike_info = f"\n**Strike Count:** {strike_count} strike(s)."
        if strikes_till_ban:
            strike_info += f" {strikes_till_ban} more strike(s) before a permanent ban."
    else:
        strike_info = f"\n**Strike Count:** {strike_count} strike(s)."

    expiry_str = f"<t:{int(expires_at.timestamp())}:R>" if expires_at else "Never"
    embed = Embed(
        title="⚠️ You have received a strike",
        description=(
            f"**Reason:** {reason}"
            f"{action_description}"
            f"{strike_info}"
            f"\n**Expires:** {expiry_str}"
        ),
        color=Color.red(),
        timestamp=now
    )
    embed.set_footer(text=f"Strike by {strike_by.display_name}", icon_url=strike_by.display_avatar.url)

    if await mysql.get_settings(user.guild.id, "dm-on-strike") == True:
        try:
            await message_user(user, "", embed=embed)
        except Exception as e:
            if interaction:
                await interaction.channel.send(user.mention, embed=embed)
            return embed

    if action:
        await perform_disciplinary_action(
            user=user,
            bot=bot,
            action_string=f"{action}:{duration_str}" if action == "timeout" else action,
            reason=reason
        )

    embed.title = f"{user.display_name} received a strike"
    settings = await mysql.get_settings(user.guild.id)
    STRIKES_CHANNEL_ID = settings.get("strike-channel") if settings else None
    if STRIKES_CHANNEL_ID is not None and log_to_channel:
        await logging.log_to_channel(embed, STRIKES_CHANNEL_ID, bot)

    return embed