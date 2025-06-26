from typing import Optional, Union
from discord import Interaction, Member, Embed, Color, Message
from discord.ext import commands
from modules.utils.discord_utils import message_user
from modules.utils.mysql import execute_query
from datetime import datetime, timedelta, timezone
from modules.utils import mod_logging
from modules.utils import mysql
from modules.utils.time import parse_duration
import discord
from discord.utils import get

def get_ban_threshold(strike_settings):
    """
    Given a settings dict mapping strike numbers to an action and duration,
    returns the strike count when a "ban" is applied (e.g., 'Ban') or None if no ban is set.
    """
    # Get the available strike thresholds as integers
    available_strikes = sorted(strike_settings.keys(), key=int)
    
    # Iterate over each strike threshold in ascending order
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

async def perform_disciplinary_action(
    user: Member,
    bot: commands.Bot,
    action_string: Union[str, list[str]],
    reason: str = None,
    source: str = "generic",
    message: Optional[Union[Message, list[Message]]] = None
) -> Optional[str]:
    """Executes one or more configured action strings on a user."""
    now = datetime.now(timezone.utc)
    results = []

    actions = [action_string] if isinstance(action_string, str) else action_string
    messages = message if isinstance(message, list) else ([message] if message else [])

    for action in actions:
        try:
            base_action, _, param = action.partition(":")
            param = param.strip() if param else None

            if base_action == "none":
                results.append("No action taken.")
                continue

            if base_action == "delete":
                if messages:
                    success = 0
                    for msg in messages:
                        try:
                            await msg.delete()
                            success += 1
                        except Exception as e:
                            print(f"[Disciplinary Action] Failed to delete message: {e}")
                    results.append(f"{success}/{len(messages)} message(s) deleted.")
                else:
                    results.append("Delete requested, but no message was provided.")
                continue

            if base_action == "strike":
                await strike(user=user, bot=bot, reason=reason, expiry=param)
                results.append(f"Strike issued{' with expiry' if param else ''}.")
                continue

            if base_action == "kick":
                await user.kick(reason=reason)
                results.append("User kicked.")
                continue

            if base_action == "ban":
                await user.ban(reason=reason)
                results.append("User banned.")
                continue

            if base_action == "timeout":
                if not param:
                    results.append("No timeout duration provided.")
                    continue
                delta = parse_duration(param)
                if not delta:
                    results.append(f"Invalid timeout duration: '{param}'")
                    continue
                until = now + delta
                await user.timeout(until, reason=reason)
                await execute_query(
                    """
                    INSERT INTO timeouts (user_id, guild_id, timeout_until, reason, source)
                    VALUES (%s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE timeout_until = VALUES(timeout_until), reason = VALUES(reason), source = VALUES(source)
                    """,
                    (user.id, user.guild.id, until, reason, source)
                )
                results.append(f"User timed out until <t:{int(until.timestamp())}:R>.")
                continue

            if base_action == "give_role":
                role = get(user.guild.roles, id=int(param)) if param and param.isdigit() else get(user.guild.roles, name=param)
                if role:
                    await user.add_roles(role, reason=reason)
                    results.append(f"Role '{role.name}' given.")
                else:
                    results.append(f"Role '{param}' not found.")
                continue

            if base_action == "take_role":
                role = get(user.guild.roles, id=int(param)) if param and param.isdigit() else get(user.guild.roles, name=param)
                if role:
                    await user.remove_roles(role, reason=reason)
                    results.append(f"Role '{role.name}' removed.")
                else:
                    results.append(f"Role '{param}' not found.")
                continue

            if base_action == "warn":
                embed = Embed(
                    title="⚠️ You Have Been Warned",
                    description = (
                        f"{user.mention}, {param}\n\n"
                        + (f"**Reason:** {reason}\n\n" if reason else "")
                        + "Please follow the server rules to avoid further action such as timeouts, strikes, or bans."
                    ),
                    color=Color.red(),
                    timestamp=now
                )
                embed.set_footer(text=f"Server: {user.guild.name}", icon_url=user.guild.icon.url if user.guild.icon else None)

                # Pick the first message if message is a list
                msg = messages[0] if messages else None

                try:
                    await user.send(embed=embed)
                    results.append("User warned via DM.")
                except discord.Forbidden:
                    if msg and msg.channel.permissions_for(msg.guild.me).send_messages:
                        await msg.channel.send(content=user.mention, embed=embed)
                        results.append("User warned via channel (DM failed).")
                    else:
                        results.append("Warning failed (couldn't send DM or channel message).")
                continue

            results.append(f"Unknown action: '{action}'")

        except Exception as e:
            print(f"[Disciplinary Action Error] {user}: {e}")
            results.append(f"Action failed: {action}")

    return "\n".join(results) if results else None

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

    reason = reason or "No reason provided"

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
    cycle_settings = await mysql.get_settings(guild_id, "cycle-strike-actions")
    available_strikes = sorted(strike_settings.keys(), key=int)

    actions = strike_settings.get(str(strike_count), [])

    # If no action is defined and cycling is enabled
    if not actions and cycle_settings:
        available_strike_values = [strike_settings[k] for k in available_strikes]
        index = (strike_count - 1) % len(available_strike_values)
        actions = available_strike_values[index]

    strikes_for_ban = get_ban_threshold(strike_settings)
    strikes_till_ban = strikes_for_ban - strike_count if strikes_for_ban is not None else None

    action_desc_parts = []
    for act in actions:
        base, _, param = act.partition(":")
        base = base.lower()
        if base == "timeout":
            dur = parse_duration(param)
            if dur is None:
                dur = timedelta(days=1)
            until = now + dur
            action_desc_parts.append(f"Timeout (ends <t:{int(until.timestamp())}:R>)")
        elif base == "ban":
            action_desc_parts.append("Ban")
        elif base == "kick":
            action_desc_parts.append("Kick")
        elif base == "delete":
            action_desc_parts.append("Delete Message")
        elif base == "give_role":
            role = user.guild.get_role(int(param)) if param and param.isdigit() else None
            name = role.name if role else param
            action_desc_parts.append(f"Give Role {name}")
        elif base == "take_role":
            role = user.guild.get_role(int(param)) if param and param.isdigit() else None
            name = role.name if role else param
            action_desc_parts.append(f"Remove Role {name}")
        elif base == "warn":
            action_desc_parts.append(f"Warn: {param}")
        elif base == "strike":
            action_desc_parts.append("Strike")
        else:
            print(f"[warn] Unrecognized action: {base}")

    if action_desc_parts:
        action_description = "\n**Actions Taken:**\n" + "\n".join(f"- {d}" for d in action_desc_parts)
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

    if await mysql.get_settings(user.guild.id, "dm-on-strike"):
        try:
            await message_user(user, "", embed=embed)
        except Exception as e:
            if interaction:
                await interaction.channel.send(user.mention, embed=embed)
            return embed

    if actions:
        await perform_disciplinary_action(
            user=user,
            bot=bot,
            action_string=actions,
            reason=reason
        )

    embed.title = f"{user.display_name} received a strike"
    STRIKES_CHANNEL_ID = await mysql.get_settings(user.guild.id, "strike-channel")
    if STRIKES_CHANNEL_ID is not None and log_to_channel:
        await mod_logging.log_to_channel(embed, STRIKES_CHANNEL_ID, bot)

    return embed