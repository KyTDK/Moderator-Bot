from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Union

import discord
from discord import Color, Embed, Interaction, Member, Message
from discord.ext import commands

from modules.i18n import get_translated_mapping
from modules.moderation.action_specs import (
    ROLE_ACTION_CANONICAL,
    get_action_spec,
)
from modules.utils import mod_logging, mysql
from modules.utils.discord_utils import message_user, resolve_role_references
from modules.utils.mysql import execute_query
from modules.utils.time import parse_duration
from modules.verification.actions import RoleAction, apply_role_actions


_logger = logging.getLogger(__name__)


DISCIPLINARY_TEXTS_FALLBACK: dict[str, str] = {
    "no_action": "No action taken.",
    "bulk_delete": "{deleted}/{total} messages bulk deleted.",
    "delete_summary": "{deleted}/{total} message(s) deleted.",
    "delete_missing": "Delete requested, but no message was provided.",
    "strike_issued": "Strike issued.",
    "strike_issued_with_expiry": "Strike issued with expiry.",
    "user_kicked": "User kicked.",
    "user_banned": "User banned.",
    "timeout_missing": "No timeout duration provided.",
    "timeout_invalid": "Invalid timeout duration: '{value}'.",
    "timeout_applied": "User timed out until <t:{timestamp}:R>.",
    "give_role_missing": "No role specified to give.",
    "give_role_not_found": "Role '{role}' not found.",
    "give_role_success": "Role '{role}' given.",
    "remove_role_missing": "No role specified to remove.",
    "remove_role_not_found": "Role '{role}' not found.",
    "remove_role_success": "Role '{role}' removed.",
    "warn_dm": "User warned via DM.",
    "warn_channel": "User warned via channel (DM failed).",
    "warn_failed": "Warning failed (couldn't send DM or channel message).",
    "broadcast_missing": "No broadcast message provided.",
    "broadcast_sent": "Broadcast message sent.",
    "broadcast_failed": "Broadcast failed.",
    "broadcast_no_channel": "No valid channel found for broadcast.",
    "unknown_action": "Unknown action: '{action}'.",
    "action_failed": "Action failed: {action}.",
}


STRIKE_TEXTS_FALLBACK: dict[str, str] = {
    "default_reason": "No reason provided",
    "embed_title_user": "You have received a strike",
    "embed_title_public": "{name} received a strike",
    "actions_heading": "**Actions Taken:**",
    "action_none": "**Action Taken:** No action applied",
    "action_item": "- {action}",
    "action_timeout": "Timeout (ends <t:{timestamp}:R>)",
    "action_ban": "Ban",
    "action_kick": "Kick",
    "action_delete": "Delete Message",
    "action_give_role": "Give Role {role}",
    "action_remove_role": "Remove Role {role}",
    "action_warn": "Warn: {message}",
    "action_broadcast": "Broadcast to {channel}: {message}",
    "action_strike": "Strike",
    "strike_count": "**Strike Count:** {count} strike(s).",
    "strike_until_ban": "{remaining} more strike(s) before a permanent ban.",
    "reason": "**Reason:** {reason}",
    "expires": "**Expires:** {expiry}",
    "issued_by": "Issued By",
    "expiry_never": "Never",
    "footer": "Server: {server}",
}


STRIKE_ERRORS_FALLBACK: dict[str, str] = {
    "too_many_strikes": "You cannot give the same player more than 100 strikes. Use `/strikes clear <user>` to reset their strikes.",
}


WARN_EMBED_FALLBACK: dict[str, str] = {
    "title": "⚠️ You Have Been Warned",
    "description": "{mention}, {message}\n\n{reason_block}{reminder}",
    "reason_block": "**Reason:** {reason}\n\n",
    "reminder": "Please follow the server rules to avoid further action such as timeouts, strikes, or bans.",
    "footer": "Server: {server}",
}

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
    message: Optional[Union[Message, list[Message]]] = None,
) -> Optional[str]:
    """Executes one or more configured action strings on a user."""

    now = datetime.now(timezone.utc)
    results: list[str] = []

    actions = [action_string] if isinstance(action_string, str) else action_string
    messages = message if isinstance(message, list) else ([message] if message else [])

    disciplinary_texts = get_translated_mapping(
        bot,
        "modules.moderation.strike.disciplinary",
        DISCIPLINARY_TEXTS_FALLBACK,
        guild_id=user.guild.id,
    )

    for action in actions:
        try:
            base_action, _, param = action.partition(":")
            base_action = base_action.strip()
            normalized_action = base_action.lower()
            param = param.strip() if param else None
            spec = get_action_spec(normalized_action)
            canonical_action = spec.canonical_name if spec else normalized_action

            if canonical_action == "none":
                results.append(disciplinary_texts["no_action"])
                continue

            if canonical_action == "delete":
                if messages:
                    first = messages[0]
                    if all(msg.channel.id == first.channel.id for msg in messages):
                        ids_to_delete = {m.id for m in messages}
                        try:
                            deleted = await first.channel.purge(
                                check=lambda m: m.id in ids_to_delete,
                                bulk=True,
                            )
                            results.append(
                                disciplinary_texts["bulk_delete"].format(
                                    deleted=len(deleted), total=len(messages)
                                )
                            )
                            continue
                        except discord.HTTPException as exc:
                            guild_text = getattr(first.guild, "id", None) or "unknown"
                            channel_text = getattr(first.channel, "id", None) or "unknown"
                            print(f"[Bulk Delete] Failed for guild {guild_text}, channel {channel_text}: {exc}")

                    success = 0
                    for msg in messages:
                        try:
                            await msg.delete()
                            success += 1
                        except Exception as exc:  # pragma: no cover - network failure
                            guild_text = getattr(msg.guild, "id", None) or "unknown"
                            channel_text = getattr(msg.channel, "id", None) or "unknown"
                            print(
                                f"[Delete] Failed for {msg.id} (guild {guild_text}, channel {channel_text}): {exc}"
                            )
                    results.append(
                        disciplinary_texts["delete_summary"].format(
                            deleted=success, total=len(messages)
                        )
                    )
                else:
                    results.append(disciplinary_texts["delete_missing"])
                continue

            if canonical_action == "strike":
                await strike(user=user, bot=bot, reason=reason, expiry=param)
                key = "strike_issued_with_expiry" if param else "strike_issued"
                results.append(disciplinary_texts[key])
                continue

            if canonical_action == "kick":
                await user.kick(reason=reason)
                results.append(disciplinary_texts["user_kicked"])
                continue

            if canonical_action == "ban":
                await user.ban(reason=reason)
                results.append(disciplinary_texts["user_banned"])
                continue

            if canonical_action == "timeout":
                if not param:
                    results.append(disciplinary_texts["timeout_missing"])
                    continue
                delta = parse_duration(param)
                if not delta:
                    results.append(
                        disciplinary_texts["timeout_invalid"].format(value=param)
                    )
                    continue
                until = now + delta
                await user.timeout(until, reason=reason)
                if source == "pfp":
                    await execute_query(
                        """
                        INSERT INTO timeouts (user_id, guild_id, timeout_until, reason, source)
                        VALUES (%s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE timeout_until = VALUES(timeout_until), reason = VALUES(reason), source = VALUES(source)
                        """,
                        (user.id, user.guild.id, until, reason, source),
                    )
                results.append(
                    disciplinary_texts["timeout_applied"].format(
                        timestamp=int(until.timestamp())
                    )
                )
                continue

            if canonical_action in ROLE_ACTION_CANONICAL:
                if not param:
                    key = (
                        "give_role_missing"
                        if canonical_action == "give_role"
                        else "remove_role_missing"
                    )
                    results.append(disciplinary_texts[key])
                    continue

                roles = resolve_role_references(user.guild, [param], logger=_logger)
                if not roles:
                    key = (
                        "give_role_not_found"
                        if canonical_action == "give_role"
                        else "remove_role_not_found"
                    )
                    results.append(
                        disciplinary_texts[key].format(role=param)
                    )
                    continue

                role_actions = [
                    RoleAction(
                        operation=canonical_action,
                        role_id=role.id,
                    )
                    for role in roles
                ]
                executed = await apply_role_actions(
                    user,
                    role_actions,
                    reason=reason,
                    logger=_logger,
                )
                executed_ids = {
                    int(action.split(":", 1)[1])
                    for action in executed
                    if ":" in action
                }
                success_roles = [role for role in roles if role.id in executed_ids]
                if success_roles:
                    key = (
                        "give_role_success"
                        if canonical_action == "give_role"
                        else "remove_role_success"
                    )
                    for role in success_roles:
                        results.append(
                            disciplinary_texts[key].format(role=role.name)
                        )
                continue

            if canonical_action == "warn":
                warn_texts = get_translated_mapping(
                    bot,
                    "modules.moderation.strike.warn_embed",
                    WARN_EMBED_FALLBACK,
                    guild_id=user.guild.id,
                )
                reason_block = (
                    warn_texts["reason_block"].format(reason=reason)
                    if reason
                    else ""
                )
                embed = Embed(
                    title=warn_texts["title"],
                    description=warn_texts["description"].format(
                        mention=user.mention,
                        message=param or "",
                        reason_block=reason_block,
                        reminder=warn_texts["reminder"],
                    ),
                    color=Color.red(),
                    timestamp=now,
                )
                embed.set_footer(
                    text=warn_texts["footer"].format(server=user.guild.name),
                    icon_url=user.guild.icon.url if user.guild.icon else None,
                )

                msg = messages[0] if messages else None

                try:
                    await user.send(embed=embed)
                    results.append(disciplinary_texts["warn_dm"])
                except discord.Forbidden:
                    if msg and msg.channel.permissions_for(msg.guild.me).send_messages:
                        await msg.channel.send(content=user.mention, embed=embed)
                        results.append(disciplinary_texts["warn_channel"])
                    else:
                        results.append(disciplinary_texts["warn_failed"])
                continue

            if canonical_action == "broadcast":
                if not param:
                    results.append(disciplinary_texts["broadcast_missing"])
                    continue

                channel_id_str, sep, message_text = param.partition("|")
                channel_id = int(channel_id_str) if channel_id_str.isdigit() else None

                if not channel_id or not sep or not message_text:
                    results.append(disciplinary_texts["broadcast_no_channel"])
                    continue

                target_channel = user.guild.get_channel(channel_id)
                if target_channel is None:
                    target_channel = bot.get_channel(channel_id)

                if target_channel and target_channel.permissions_for(user.guild.me).send_messages:
                    try:
                        await target_channel.send(message_text)
                        results.append(disciplinary_texts["broadcast_sent"])
                    except Exception as exc:  # pragma: no cover - network failure
                        print(f"[Broadcast] Failed to send message: {exc}")
                        results.append(disciplinary_texts["broadcast_failed"])
                else:
                    results.append(disciplinary_texts["broadcast_no_channel"])
                continue

            results.append(
                disciplinary_texts["unknown_action"].format(action=action)
            )

        except Exception as exc:  # pragma: no cover - defensive logging
            print(f"[Disciplinary Action Error] {user}: {exc}")
            results.append(
                disciplinary_texts["action_failed"].format(action=action)
            )

    return "\n".join(results) if results else None

async def strike(
    user: Member,
    bot: commands.Bot,
    reason: str = "No reason provided",
    interaction: Optional[Interaction] = None,
    expiry: Optional[str] = None,
    log_to_channel: bool = True,
) -> discord.Embed:
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

    action_desc_parts: list[str] = []
    for act in actions:
        base, _, param = act.partition(":")
        base = base.lower()
        if base == "timeout":
            dur = parse_duration(param)
            if dur is None:
                dur = timedelta(days=1)
            until = now + dur
            action_desc_parts.append(
                strike_texts["action_timeout"].format(
                    timestamp=int(until.timestamp())
                )
            )
        elif base == "ban":
            action_desc_parts.append(strike_texts["action_ban"])
        elif base == "kick":
            action_desc_parts.append(strike_texts["action_kick"])
        elif base == "delete":
            action_desc_parts.append(strike_texts["action_delete"])
        elif base == "give_role":
            role = user.guild.get_role(int(param)) if param and param.isdigit() else None
            name = role.name if role else param
            action_desc_parts.append(
                strike_texts["action_give_role"].format(role=name)
            )
        elif base in {"take_role", "remove_role"}:
            role = user.guild.get_role(int(param)) if param and param.isdigit() else None
            name = role.name if role else param
            action_desc_parts.append(
                strike_texts["action_remove_role"].format(role=name)
            )
        elif base == "warn":
            action_desc_parts.append(
                strike_texts["action_warn"].format(message=param)
            )
        elif base == "broadcast":
            channel_id_str, sep, message_text = param.partition("|")
            channel_id = int(channel_id_str) if channel_id_str.isdigit() else None
            channel_obj = None
            if channel_id:
                channel_obj = user.guild.get_channel(channel_id)
            channel_label = (
                channel_obj.mention
                if channel_obj and hasattr(channel_obj, "mention")
                else (f"<#{channel_id}>" if channel_id else channel_id_str)
            )
            action_desc_parts.append(
                strike_texts["action_broadcast"].format(
                    channel=channel_label,
                    message=message_text if sep else param,
                )
            )
        elif base == "strike":
            action_desc_parts.append(strike_texts["action_strike"])
        else:
            print(f"[warn] Unrecognized action: {base}")

    if action_desc_parts:
        action_description = (
            "\n"
            + strike_texts["actions_heading"]
            + "\n"
            + "\n".join(
                strike_texts["action_item"].format(action=desc)
                for desc in action_desc_parts
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

    if actions:
        await perform_disciplinary_action(
            user=user,
            bot=bot,
            action_string=actions,
            reason=reason,
        )

    embed.title = strike_texts["embed_title_public"].format(name=user.display_name)
    strikes_channel_id = await mysql.get_settings(user.guild.id, "strike-channel")
    if strikes_channel_id is not None and log_to_channel:
        await mod_logging.log_to_channel(embed, strikes_channel_id, bot)

    return embed
