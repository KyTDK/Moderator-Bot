from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Optional, Sequence

import discord

from modules.nsfw_scanner.constants import LOG_CHANNEL_ID
from modules.utils.discord_utils import safe_get_channel

log = logging.getLogger(__name__)

_SEVERITY_META: dict[str, tuple[str, discord.Color]] = {
    "debug": (":grey_question:", discord.Color.dark_grey()),
    "info": (":information_source:", discord.Color.dark_grey()),
    "notice": (":speech_balloon:", discord.Color.blue()),
    "success": (":white_check_mark:", discord.Color.green()),
    "warning": (":warning:", discord.Color.orange()),
    "error": (":rotating_light:", discord.Color.red()),
    "critical": (":rotating_light:", discord.Color.red()),
}


@dataclass(slots=True)
class DeveloperLogField:
    """Structured embed field describing developer log channel metadata."""

    name: str
    value: str
    inline: bool = False


def _context_suffix(context: str | None) -> str:
    return f" for {context}" if context else ""


def _normalize_severity(value: str | None, *, default: str = "info") -> str:
    if not value:
        return default
    normalized = value.lower().strip()
    if normalized not in _SEVERITY_META:
        return default
    return normalized


async def resolve_log_channel(
    bot: discord.Client,
    *,
    logger: Optional[logging.Logger] = None,
    context: str | None = None,
    raise_on_exception: bool = False,
) -> Optional[discord.abc.Messageable]:
    """Resolve the configured developer log channel, returning ``None`` when unavailable."""
    if not LOG_CHANNEL_ID:
        return None

    target_logger = logger or log
    try:
        channel = await safe_get_channel(bot, LOG_CHANNEL_ID)
    except Exception as exc:
        target_logger.debug(
            "Failed to resolve LOG_CHANNEL_ID=%s%s",
            LOG_CHANNEL_ID,
            _context_suffix(context),
            exc_info=True,
        )
        if raise_on_exception:
            raise
        return None

    if channel is None:
        target_logger.debug(
            "LOG_CHANNEL_ID=%s%s resolved to None",
            LOG_CHANNEL_ID,
            _context_suffix(context),
        )
        return None

    return channel


async def send_developer_log_message(
    bot: discord.Client,
    *,
    content: str | None = None,
    embed: discord.Embed | None = None,
    allowed_mentions: discord.AllowedMentions | None = None,
    logger: Optional[logging.Logger] = None,
    context: str | None = None,
) -> bool:
    """Send a plain developer log message to ``LOG_CHANNEL_ID``.

    Use this helper strictly for operator/developer alerts. For end-user messaging,
    prefer the general utilities in ``modules.utils.mod_logging``.
    """
    channel = await resolve_log_channel(bot, logger=logger, context=context)
    if channel is None:
        return False

    target_logger = logger or log
    try:
        await channel.send(
            content=content,
            embed=embed,
            allowed_mentions=allowed_mentions,
        )
    except Exception:
        target_logger.debug(
            "Failed to send payload to LOG_CHANNEL_ID=%s%s",
            LOG_CHANNEL_ID,
            _context_suffix(context),
            exc_info=True,
        )
        return False

    return True


def build_developer_log_embed(
    *,
    title: str,
    description: str | None = None,
    severity: str | None = None,
    fields: Sequence[DeveloperLogField] | None = None,
    footer: str | None = None,
    timestamp: bool = False,
    color: discord.Color | None = None,
) -> discord.Embed:
    """Construct an embed tailored to the developer log channel layout."""
    normalized = _normalize_severity(severity, default="info")
    _, severity_color = _SEVERITY_META[normalized]
    embed_color = color or severity_color
    embed = discord.Embed(title=title, description=description, color=embed_color)
    if timestamp:
        embed.timestamp = discord.utils.utcnow()
    for field in fields or ():
        embed.add_field(name=field.name, value=field.value, inline=field.inline)
    if footer:
        embed.set_footer(text=footer)
    return embed


async def send_developer_log_embed(
    bot: discord.Client,
    *,
    embed: discord.Embed,
    content: str | None = None,
    context: str | None = None,
    logger: Optional[logging.Logger] = None,
    allowed_mentions: discord.AllowedMentions | None = None,
) -> bool:
    """Send a prepared embed to the developer log channel."""
    mentions = allowed_mentions or discord.AllowedMentions.none()
    return await send_developer_log_message(
        bot,
        content=content,
        embed=embed,
        allowed_mentions=mentions,
        logger=logger,
        context=context,
    )


def _compose_content(
    *,
    summary: str,
    severity: str,
    content: str | None,
    mention: str | None,
) -> tuple[str | None, discord.AllowedMentions]:
    normalized = _normalize_severity(severity, default="info")
    emoji, _ = _SEVERITY_META[normalized]
    base = content or f"{emoji} {summary}"
    if mention:
        combined = f"{mention} {base}".strip()
        # Allow pings for users/roles; include everyone mentions when explicitly requested.
        if "@everyone" in mention or "@here" in mention:
            allowed = discord.AllowedMentions(everyone=True, users=True, roles=True)
        else:
            allowed = discord.AllowedMentions(users=True, roles=True)
        return combined, allowed
    return base, discord.AllowedMentions.none()


async def log_to_developer_channel(
    bot: discord.Client,
    *,
    summary: str,
    severity: str | None = None,
    description: str | None = None,
    fields: Sequence[DeveloperLogField] | None = None,
    footer: str | None = None,
    timestamp: bool = False,
    mention: str | None = None,
    content: str | None = None,
    context: str | None = None,
    logger: Optional[logging.Logger] = None,
    color: discord.Color | None = None,
) -> bool:
    """Send a structured developer-facing log entry to ``LOG_CHANNEL_ID``."""
    normalized = _normalize_severity(severity, default="info")
    embed_needed = any(
        (
            description,
            fields,
            footer,
            timestamp,
            color is not None,
        )
    )
    embed = None
    if embed_needed:
        embed = build_developer_log_embed(
            title=summary,
            description=description,
            severity=normalized,
            fields=fields,
            footer=footer,
            timestamp=timestamp,
            color=color,
        )
    message_content, allowed_mentions = _compose_content(
        summary=summary,
        severity=normalized,
        content=content,
        mention=mention,
    )
    return await send_developer_log_message(
        bot,
        content=message_content,
        embed=embed,
        allowed_mentions=allowed_mentions,
        logger=logger,
        context=context,
    )


async def log_developer_issue(
    bot: discord.Client,
    *,
    summary: str,
    details: str | None = None,
    severity: str | None = None,
    context: str | None = None,
    logger: Optional[logging.Logger] = None,
) -> bool:
    """Send a standardized serious issue notification to the developer log channel."""
    normalized = _normalize_severity(severity, default="error")
    success = await log_to_developer_channel(
        bot,
        summary=summary,
        severity=normalized,
        description=details,
        context=context,
        logger=logger,
    )
    if not success:
        target_logger = logger or log
        target_logger.warning(
            "Failed to deliver developer log (%s): %s",
            normalized,
            summary,
        )
    return success


__all__ = [
    "DeveloperLogField",
    "build_developer_log_embed",
    "log_developer_issue",
    "log_to_developer_channel",
    "resolve_log_channel",
    "send_developer_log_embed",
    "send_developer_log_message",
]
