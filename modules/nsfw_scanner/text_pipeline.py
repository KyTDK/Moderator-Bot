from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, TYPE_CHECKING

import discord
from discord.ext import commands
from discord.utils import escape_markdown, escape_mentions

from modules.nsfw_scanner.settings_keys import (
    NSFW_TEXT_ACTION_SETTING,
    NSFW_TEXT_ENABLED_SETTING,
    NSFW_TEXT_SEND_EMBED_SETTING,
    NSFW_TEXT_STRIKES_ONLY_SETTING,
)
from modules.utils import mod_logging, mysql
import modules.utils.log_channel as log_channel

# Provide module-level alias so existing monkeypatches on TextScanPipeline can override it.
send_log_message = log_channel.send_log_message

from modules.nsfw_scanner.helpers import process_text
from modules.nsfw_scanner.constants import LOG_CHANNEL_ID
from modules.nsfw_scanner.scanner_utils import to_bool

if TYPE_CHECKING:
    from modules.nsfw_scanner.scanner import NSFWScanner
    from modules.nsfw_scanner.helpers.attachments import AttachmentSettingsCache

log = logging.getLogger(__name__)


def _build_text_verbose_embed(
    *,
    author: discord.abc.User | None,
    channel: discord.abc.Messageable | None,
    guild_id: int | None,
    text_content: str,
    result: dict[str, Any] | None,
    message: discord.Message | None,
    debug_lines: list[str] | None = None,
) -> discord.Embed:
    sanitized = escape_mentions(escape_markdown(text_content.strip()))
    snippet = sanitized[:512]
    if len(sanitized) > 512:
        snippet = sanitized[:509].rstrip() + "..."

    is_flagged = bool(result and result.get("is_nsfw"))
    decision_label = "Flagged" if is_flagged else "Allowed"
    color = discord.Color.red() if is_flagged else discord.Color.orange()

    lines: list[str] = []
    if author is not None:
        mention = getattr(author, "mention", None)
        if not mention:
            author_id = getattr(author, "id", None)
            mention = f"<@{author_id}>" if author_id else "Unknown user"
        lines.append(f"User: {mention}")
    if channel is not None:
        channel_name = getattr(channel, "mention", None) or getattr(channel, "name", None)
        channel_id = getattr(channel, "id", None)
        if channel_name and channel_id:
            lines.append(f"Channel: {channel_name} (`{channel_id}`)")
        elif channel_id is not None:
            lines.append(f"Channel ID: `{channel_id}`")
    if message is not None and getattr(message, "jump_url", None):
        lines.append(f"[Jump to message]({message.jump_url})")
    if guild_id is not None:
        lines.append(f"Guild ID: `{guild_id}`")
    lines.append(f"Decision: **{decision_label}**")

    embed = discord.Embed(
        title="NSFW Text Scan Report",
        description="\n".join(lines),
        color=color,
    )

    if snippet:
        code_block = snippet.replace("```", "`\u200b``")
        embed.add_field(
            name="Content Snippet",
            value=f"```{code_block}```",
            inline=False,
        )

    if isinstance(result, dict):
        category = result.get("category")
        if category:
            embed.add_field(name="Category", value=str(category), inline=True)
        reason = result.get("reason")
        if reason:
            embed.add_field(name="Reason", value=str(reason)[:1024], inline=True)
        score = result.get("score")
        if score is not None:
            try:
                embed.add_field(name="Score", value=f"{float(score):.3f}", inline=True)
            except (TypeError, ValueError):
                embed.add_field(name="Score", value=str(score), inline=True)
        similarity = result.get("similarity")
        if similarity is not None:
            try:
                embed.add_field(name="Similarity", value=f"{float(similarity):.3f}", inline=True)
            except (TypeError, ValueError):
                embed.add_field(name="Similarity", value=str(similarity), inline=True)
        threshold = result.get("threshold") or result.get("text_threshold")
        if threshold is not None:
            try:
                embed.add_field(name="Threshold", value=f"{float(threshold):.3f}", inline=True)
            except (TypeError, ValueError):
                embed.add_field(name="Threshold", value=str(threshold), inline=True)
    if debug_lines:
        embed.add_field(
            name="Debug Context",
            value="\n".join(debug_lines)[:1024],
            inline=False,
        )

    return embed


class TextScanPipeline:
    """Encapsulates NSFW text scanning and logging behaviour."""

    def __init__(self, *, bot: commands.Bot):
        self._bot = bot

    async def scan(
        self,
        *,
        scanner: "NSFWScanner",
        message: discord.Message,
        guild_id: int | None,
        nsfw_callback: Callable[..., Awaitable[None]] | None,
        settings_cache: "AttachmentSettingsCache",
        ensure_settings_map: Callable[[], Awaitable[dict[str, Any]]],
    ) -> bool:
        text_content = (message.content or "").strip()
        if not text_content:
            return False

        settings_map: dict[str, Any] | None = None
        if guild_id is not None:
            settings_map = await ensure_settings_map()

        text_scanning_enabled = False
        send_text_embed = True
        actions_allowed = False
        accelerated_allowed: bool | None = None
        strikes_only = False
        strike_count: int | None = None

        if guild_id is not None:
            text_enabled_value = settings_map.get(NSFW_TEXT_ENABLED_SETTING) if settings_map else None
            text_scanning_enabled = to_bool(text_enabled_value, default=False)
            settings_cache.set_text_enabled(text_scanning_enabled)
        if not text_scanning_enabled:
            if guild_id is not None and LOG_CHANNEL_ID:
                embed = discord.Embed(
                    title="NSFW Text Scan Skipped",
                    description=f"Guild `{guild_id}` • message ID `{getattr(message, 'id', 'unknown')}`",
                    color=discord.Color.orange(),
                )
                embed.add_field(
                    name="Reason",
                    value="NSFW text scanning is disabled by settings.",
                    inline=False,
                )
                embed.add_field(
                    name="Raw Setting Value",
                    value=repr(text_enabled_value),
                    inline=False,
                )
                embed.add_field(
                    name="Settings Map Keys",
                    value=", ".join(sorted(settings_map.keys())) if settings_map else "(none)",
                    inline=False,
                )
                allowed_mentions = None
                if hasattr(discord, "AllowedMentions") and hasattr(discord.AllowedMentions, "none"):
                    allowed_mentions = discord.AllowedMentions.none()
                try:
                    await send_log_message(
                        self._bot,
                        embed=embed,
                        allowed_mentions=allowed_mentions,
                        context="nsfw_scanner.text_scan_skipped",
                    )
                except Exception:
                    pass
            return False

        if guild_id is not None and text_scanning_enabled:
            if settings_cache.has_accelerated():
                accelerated_allowed = bool(settings_cache.get_accelerated())
            else:
                try:
                    accelerated_allowed = bool(await mysql.is_accelerated(guild_id=guild_id))
                except Exception:
                    accelerated_allowed = False
                settings_cache.set_accelerated(accelerated_allowed)

            actions_allowed = to_bool(accelerated_allowed, default=False)

            strikes_only = to_bool(
                (settings_map or {}).get(NSFW_TEXT_STRIKES_ONLY_SETTING),
                default=False,
            )
            if strikes_only:
                author_id = getattr(getattr(message, "author", None), "id", None)
                strike_count = 0
                if author_id is not None:
                    try:
                        strike_count = await mysql.get_strike_count(author_id, guild_id)
                    except Exception:
                        strike_count = 0
                if strike_count <= 0:
                    actions_allowed = False

            send_text_embed = to_bool(
                (settings_map or {}).get(NSFW_TEXT_SEND_EMBED_SETTING),
                default=True,
            )
        else:
            actions_allowed = False

        debug_lines: list[str] = []
        if accelerated_allowed is not None:
            debug_lines.append(f"Accelerated plan: {'yes' if accelerated_allowed else 'no'}")
        debug_lines.append(f"Actions allowed: {'yes' if actions_allowed else 'no'}")
        if strikes_only:
            debug_lines.append("Strikes-only mode: yes")
            if strike_count is not None:
                debug_lines.append(f"User strike count: {strike_count}")
        else:
            debug_lines.append("Strikes-only mode: no")
        debug_lines.append(f"Send moderation embed: {'yes' if send_text_embed else 'no'}")

        author_id = getattr(getattr(message, "author", None), "id", None)
        text_metadata = {
            "message_id": getattr(message, "id", None),
            "channel_id": getattr(getattr(message, "channel", None), "id", None),
            "author_id": author_id,
        }
        if author_id is not None:
            text_metadata["user_id"] = author_id
        if guild_id is not None:
            text_metadata["guild_id"] = guild_id

        text_result = await process_text(
            scanner,
            text_content,
            guild_id=guild_id,
            settings=settings_map,
            payload_metadata=text_metadata,
        )

        verbose_enabled = False
        if message is not None and guild_id is not None:
            if settings_cache.has_verbose():
                verbose_enabled = bool(settings_cache.get_verbose())
            else:
                try:
                    verbose_enabled = bool(
                        await mysql.get_settings(guild_id, "nsfw-verbose")
                    )
                except Exception:
                    verbose_enabled = False
                settings_cache.set_verbose(verbose_enabled)

        verbose_embed: discord.Embed | None = None
        if verbose_enabled and text_result is not None:
            verbose_embed = _build_text_verbose_embed(
                author=getattr(message, "author", None),
                channel=getattr(message, "channel", None),
                guild_id=guild_id,
                text_content=text_content,
                result=text_result,
                message=message,
                debug_lines=debug_lines if debug_lines else None,
            )

        if verbose_enabled and verbose_embed is not None:
            channel_obj = getattr(message, "channel", None)
            channel_id = getattr(channel_obj, "id", None)
            if channel_id is not None:
                try:
                    await mod_logging.log_to_channel(
                        embed=verbose_embed,
                        channel_id=channel_id,
                        bot=self._bot,
                    )
                except Exception as exc:
                    error_embed = discord.Embed(
                        title="NSFW Text Verbose Delivery Failed",
                        description=f"Channel `{channel_id}`",
                        color=discord.Color.red(),
                    )
                    error_embed.add_field(
                        name="Error",
                        value=f"`{type(exc).__name__}`: {exc}",
                        inline=False,
                    )
                    allowed_mentions = None
                    if hasattr(discord, "AllowedMentions") and hasattr(discord.AllowedMentions, "none"):
                        allowed_mentions = discord.AllowedMentions.none()
                    await send_log_message(
                        self._bot,
                        embed=error_embed,
                        allowed_mentions=allowed_mentions,
                        context="nsfw_scanner.text_verbose_failure",
                    )

            try:
                log_embed = verbose_embed.copy() if hasattr(verbose_embed, "copy") else verbose_embed
                log_embed.title = "NSFW Text Scan Debug"
                allowed_mentions = None
                if hasattr(discord, "AllowedMentions") and hasattr(discord.AllowedMentions, "none"):
                    allowed_mentions = discord.AllowedMentions.none()
                await send_log_message(
                    self._bot,
                    embed=log_embed,
                    allowed_mentions=allowed_mentions,
                    context="nsfw_scanner.text_scan",
                )
            except Exception as exc:
                error_embed = discord.Embed(
                    title="NSFW Text Scan Log Failure",
                    description="Failed to send verbose debug embed.",
                    color=discord.Color.red(),
                )
                error_embed.add_field(
                    name="Error",
                    value=f"`{type(exc).__name__}`: {exc}",
                    inline=False,
                )
                allowed_mentions = None
                if hasattr(discord, "AllowedMentions") and hasattr(discord.AllowedMentions, "none"):
                    allowed_mentions = discord.AllowedMentions.none()
                await send_log_message(
                    self._bot,
                    embed=error_embed,
                    allowed_mentions=allowed_mentions,
                    context="nsfw_scanner.text_scan_log_failure",
                )
        elif LOG_CHANNEL_ID:
            try:
                log_embed = _build_text_verbose_embed(
                    author=getattr(message, "author", None),
                    channel=getattr(message, "channel", None),
                    guild_id=guild_id,
                    text_content=text_content,
                    result=text_result or {"is_nsfw": False, "reason": "no_result"},
                    message=message,
                    debug_lines=debug_lines if debug_lines else None,
                )
                log_embed.title = "NSFW Text Scan Summary"
                allowed_mentions = None
                if hasattr(discord, "AllowedMentions") and hasattr(discord.AllowedMentions, "none"):
                    allowed_mentions = discord.AllowedMentions.none()
                await send_log_message(
                    self._bot,
                    embed=log_embed,
                    allowed_mentions=allowed_mentions,
                    context="nsfw_scanner.text_scan_summary",
                )
            except Exception:
                pass

        if not (text_result and text_result.get("is_nsfw")):
            return False

        if nsfw_callback and actions_allowed:
            category = text_result.get("category") or "unspecified"
            confidence_value = None
            confidence_source = None
            score = text_result.get("score")
            similarity = text_result.get("similarity")
            try:
                if score is not None:
                    confidence_value = float(score)
                    confidence_source = "score"
                elif similarity is not None:
                    confidence_value = float(similarity)
                    confidence_source = "similarity"
            except (TypeError, ValueError):
                confidence_value = None
                confidence_source = None

            category_label = category.replace("_", " ").title()
            reason = f"Detected potential policy violation (Category: **{category_label}**)."

            await nsfw_callback(
                message.author,
                self._bot,
                guild_id,
                reason,
                None,
                message,
                confidence=confidence_value,
                confidence_source=confidence_source,
                action_setting=NSFW_TEXT_ACTION_SETTING,
                send_embed=send_text_embed,
            )

            return True

        if text_result and text_result.get("is_nsfw") and not actions_allowed:
            reasons: list[str] = []
            if accelerated_allowed is False:
                reasons.append("Accelerated plan is not active.")
            if strikes_only and (strike_count is None or strike_count <= 0):
                reasons.append("Strikes-only mode with no prior strikes.")
            if nsfw_callback is None:
                reasons.append("No NSFW callback configured.")
            if not reasons:
                reasons.append("Actions are disabled by configuration.")

            description_parts: list[str] = []
            if guild_id is not None:
                description_parts.append(f"Guild `{guild_id}`")
            message_id = getattr(message, "id", None)
            if message_id is not None:
                description_parts.append(f"Message `{message_id}`")

            embed = discord.Embed(
                title="NSFW Text Action Skipped",
                description="\n".join(description_parts) or "Text action skipped.",
                color=discord.Color.orange(),
            )
            embed.add_field(
                name="Reason",
                value="\n".join(reasons),
                inline=False,
            )
            if text_result:
                embed.add_field(
                    name="Category",
                    value=str(text_result.get("category") or "unknown"),
                    inline=True,
                )
                embed.add_field(
                    name="Score",
                    value=str(text_result.get("score") if text_result.get("score") is not None else "n/a"),
                    inline=True,
                )
            embed.add_field(
                name="Accelerated",
                value="yes" if accelerated_allowed else "no",
                inline=True,
            )
            embed.add_field(
                name="Actions Allowed",
                value="yes" if actions_allowed else "no",
                inline=True,
            )
            embed.add_field(
                name="Strikes-Only Mode",
                value="yes" if strikes_only else "no",
                inline=True,
            )
            if strikes_only:
                embed.add_field(
                    name="Strike Count",
                    value=str(strike_count or 0),
                    inline=True,
                )
            if debug_lines:
                embed.add_field(
                    name="Debug Context",
                    value="\n".join(debug_lines)[:1024],
                    inline=False,
                )

            allowed_mentions = None
            if hasattr(discord, "AllowedMentions") and hasattr(discord.AllowedMentions, "none"):
                allowed_mentions = discord.AllowedMentions.none()
            try:
                await send_log_message(
                    self._bot,
                    embed=embed,
                    allowed_mentions=allowed_mentions,
                    context="nsfw_scanner.text_actions_blocked",
                )
            except Exception:
                pass

        return actions_allowed


__all__ = ["TextScanPipeline"]
