from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional, Union

import discord
from discord import Embed, Message
from discord.ext import commands

from modules.i18n import get_translated_mapping
from modules.moderation.action_specs import (
    ROLE_ACTION_CANONICAL,
    get_action_spec,
)
from modules.utils import mysql
from modules.utils.discord_utils import resolve_role_references, safe_get_channel
from modules.utils.mysql import execute_query
from modules.utils.time import parse_duration
from modules.verification.actions import RoleAction, apply_role_actions

from .texts import DISCIPLINARY_TEXTS_FALLBACK, WARN_EMBED_FALLBACK

_logger = logging.getLogger(__name__)


async def perform_disciplinary_action(
    user: discord.Member,
    bot: commands.Bot,
    action_string: Union[str, list[str]],
    reason: str | None = None,
    source: str = "generic",
    message: Optional[Union[Message, list[Message]]] = None,
) -> Optional[str]:
    """Execute one or more configured action strings on a user."""

    now = datetime.now(timezone.utc)
    results: list[str] = []

    raw_actions = [action_string] if isinstance(action_string, str) else action_string
    actions = list(raw_actions)
    messages = message if isinstance(message, list) else ([message] if message else [])

    channel_cache: dict[int, object] = {}
    message_cache: dict[tuple[int, int], Message] = {}

    disciplinary_texts = get_translated_mapping(
        bot,
        "modules.moderation.strike.disciplinary",
        DISCIPLINARY_TEXTS_FALLBACK,
        guild_id=user.guild.id,
    )

    def _extract_channel_id(msg) -> int | None:
        if msg is None:
            return None
        channel = getattr(msg, "channel", None)
        channel_id = getattr(channel, "id", None)
        if channel_id is not None:
            return channel_id
        return getattr(msg, "channel_id", None)

    def _extract_message_id(msg) -> int | None:
        if msg is None:
            return None
        message_id = getattr(msg, "id", None)
        if message_id is not None:
            return message_id
        return getattr(msg, "message_id", None)

    def _extract_guild_id(msg) -> int | None:
        if msg is None:
            return None
        guild = getattr(msg, "guild", None)
        guild_id = getattr(guild, "id", None)
        if guild_id is not None:
            return guild_id
        return getattr(msg, "guild_id", None)

    async def _resolve_channel_for_message(msg):
        channel_id = _extract_channel_id(msg)
        if channel_id is None:
            return None
        if channel_id in channel_cache:
            return channel_cache[channel_id]
        channel = bot.get_channel(channel_id)
        if channel is not None:
            channel_cache[channel_id] = channel
            return channel
        channel = await safe_get_channel(bot, channel_id)
        if channel is not None:
            channel_cache[channel_id] = channel
        return channel

    async def _resolve_message_for_deletion(msg):
        if msg is None:
            return None
        if hasattr(msg, "delete") and getattr(msg, "channel", None) is not None:
            return msg
        channel_id = _extract_channel_id(msg)
        message_id = _extract_message_id(msg)
        if channel_id is None or message_id is None:
            return None
        key = (channel_id, message_id)
        if key in message_cache:
            return message_cache[key]
        channel = await _resolve_channel_for_message(msg)
        if channel is None or not hasattr(channel, "fetch_message"):
            return None
        try:
            fetched = await channel.fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden):
            return None
        except discord.HTTPException as exc:
            print(f"[Delete] fetch_message({message_id}) failed in channel {channel_id}: {exc}")
            return None
        message_cache[key] = fetched
        return fetched

    def _format_action_failure(action_label, error=None):
        message_text = disciplinary_texts["action_failed"].format(action=action_label)
        if error is None:
            return message_text
        if isinstance(error, str):
            reason_text = error.strip()
        else:
            if isinstance(error, discord.Forbidden):
                reason_text = "Missing Permissions"
            elif isinstance(error, discord.NotFound):
                reason_text = "Target not found"
            elif isinstance(error, discord.HTTPException):
                text = (error.text or "").strip()
                reason_text = text or f"HTTP {error.status}"
            else:
                reason_text = str(error).strip() or error.__class__.__name__
        if not reason_text:
            return message_text
        trimmed_message = message_text.rstrip()
        suffix = ""
        if trimmed_message.endswith("."):
            trimmed_message = trimmed_message[:-1]
            suffix = "."
        return f"{trimmed_message} ({reason_text}){suffix}"

    if len(actions) > 1:
        guild = getattr(user, "guild", None)
        guild_id = getattr(guild, "id", None)
        accelerated = False
        if guild_id is not None:
            try:
                accelerated = await mysql.is_accelerated(guild_id=guild_id)
            except Exception:
                accelerated = False
        if not accelerated:
            _logger.debug(
                "Guild %s lacks Accelerated; limiting disciplinary actions to the first entry.",
                guild_id,
            )
            actions = actions[:1]

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
                    resolved_messages: list[Message] = []
                    for msg in messages:
                        resolved = await _resolve_message_for_deletion(msg)
                        if resolved is not None:
                            resolved_messages.append(resolved)
                    total_requested = len(messages)
                    missing_count = total_requested - len(resolved_messages)

                    if resolved_messages:
                        first_resolved = resolved_messages[0]
                        first_channel = getattr(first_resolved, "channel", None)
                        same_channel = (
                            first_channel is not None
                            and all(
                                getattr(m.channel, "id", None) == getattr(first_channel, "id", None)
                                for m in resolved_messages
                            )
                        )
                        if (
                            same_channel
                            and len(resolved_messages) > 1
                            and hasattr(first_channel, "purge")
                        ):
                            ids_to_delete = {m.id for m in resolved_messages}
                            try:
                                deleted = await first_channel.purge(
                                    check=lambda m: m.id in ids_to_delete,
                                    bulk=True,
                                )
                                results.append(
                                    disciplinary_texts["bulk_delete"].format(
                                        deleted=len(deleted),
                                        total=total_requested,
                                    )
                                )
                                if missing_count:
                                    results.append(
                                        _format_action_failure(
                                            action,
                                            f"{missing_count} message(s) unavailable for deletion",
                                        )
                                    )
                                continue
                            except (discord.Forbidden, discord.HTTPException) as exc:
                                guild_text = getattr(first_resolved.guild, "id", None) or _extract_guild_id(first_resolved) or "unknown"
                                channel_text = getattr(first_channel, "id", None) or _extract_channel_id(first_resolved) or "unknown"
                                print(
                                    f"[Bulk Delete] Failed for guild {guild_text}, channel {channel_text}: {exc}"
                                )
                                results.append(_format_action_failure(action, exc))
                            except Exception as exc:  # pragma: no cover - defensive logging
                                guild_text = getattr(first_resolved.guild, "id", None) or _extract_guild_id(first_resolved) or "unknown"
                                channel_text = getattr(first_channel, "id", None) or _extract_channel_id(first_resolved) or "unknown"
                                print(
                                    f"[Bulk Delete] Unexpected failure for guild {guild_text}, channel {channel_text}: {exc}"
                                )
                                results.append(_format_action_failure(action, exc))

                        success = 0
                        failure_exc = None
                        for resolved_msg in resolved_messages:
                            try:
                                await resolved_msg.delete()
                                success += 1
                            except Exception as exc:  # pragma: no cover - network failure
                                guild_text = getattr(resolved_msg.guild, "id", None) or _extract_guild_id(resolved_msg) or "unknown"
                                channel_text = getattr(resolved_msg.channel, "id", None) or _extract_channel_id(resolved_msg) or "unknown"
                                message_id = getattr(resolved_msg, "id", None) or "unknown"
                                print(
                                    f"[Delete] Failed for {message_id} (guild {guild_text}, channel {channel_text}): {exc}"
                                )
                                if failure_exc is None:
                                    failure_exc = exc
                        if failure_exc is not None:
                            results.append(_format_action_failure(action, failure_exc))
                        results.append(
                            disciplinary_texts["delete_summary"].format(
                                deleted=success,
                                total=total_requested,
                            )
                        )
                        if missing_count:
                            results.append(
                                _format_action_failure(
                                    action,
                                    f"{missing_count} message(s) unavailable for deletion",
                                )
                            )
                    else:
                        results.append(disciplinary_texts["delete_missing"])
                else:
                    results.append(disciplinary_texts["delete_missing"])
                continue

            if canonical_action == "strike":
                try:
                    from .service import strike as issue_strike

                    await issue_strike(user=user, bot=bot, reason=reason, expiry=param)
                except Exception as exc:
                    results.append(_format_action_failure(action, exc))
                else:
                    key = "strike_issued_with_expiry" if param else "strike_issued"
                    results.append(disciplinary_texts[key])
                continue

            if canonical_action == "kick":
                try:
                    await user.kick(reason=reason)
                except Exception as exc:
                    results.append(_format_action_failure(action, exc))
                else:
                    results.append(disciplinary_texts["user_kicked"])
                continue

            if canonical_action == "ban":
                try:
                    await user.ban(reason=reason)
                except Exception as exc:
                    results.append(_format_action_failure(action, exc))
                else:
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
                try:
                    await user.timeout(until, reason=reason)
                except Exception as exc:
                    results.append(_format_action_failure(action, exc))
                    continue
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
                try:
                    executed = await apply_role_actions(
                        user,
                        role_actions,
                        reason=reason,
                        logger=_logger,
                    )
                except Exception as exc:
                    results.append(_format_action_failure(action, exc))
                    continue
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
                missing_roles = [role for role in roles if role.id not in executed_ids]
                if missing_roles:
                    label = ", ".join(role.name for role in missing_roles)
                    results.append(
                        _format_action_failure(action, f"Unable to update: {label}")
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
                    color=discord.Color.red(),
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
                    channel = await _resolve_channel_for_message(msg) if msg else None
                    me_member = getattr(user.guild, "me", None)
                    can_send = (
                        channel is not None
                        and me_member is not None
                        and hasattr(channel, "permissions_for")
                        and channel.permissions_for(me_member).send_messages
                    )
                    if can_send:
                        await channel.send(content=user.mention, embed=embed)
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
                        results.append(_format_action_failure(action, exc))
                else:
                    results.append(disciplinary_texts["broadcast_no_channel"])
                continue

            results.append(
                disciplinary_texts["unknown_action"].format(action=action)
            )

        except Exception as exc:  # pragma: no cover - defensive logging
            print(f"[Disciplinary Action Error] {user}: {exc}")
            results.append(_format_action_failure(action, exc))

    return "\n".join(results) if results else None


__all__ = ["perform_disciplinary_action"]
