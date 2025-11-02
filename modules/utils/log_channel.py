from __future__ import annotations

import logging
from typing import Optional

import discord

from modules.nsfw_scanner.constants import LOG_CHANNEL_ID
from modules.utils.discord_utils import safe_get_channel

log = logging.getLogger(__name__)

_SERIOUS_SEVERITY_META: dict[str, tuple[str, discord.Color]] = {
    "info": (":information_source:", discord.Color.dark_grey()),
    "warning": (":warning:", discord.Color.orange()),
    "error": (":rotating_light:", discord.Color.red()),
    "critical": (":rotating_light:", discord.Color.red()),
}


def _normalize_severity(value: str | None) -> str:
    if not value:
        return "error"
    normalized = value.lower().strip()
    if normalized not in _SERIOUS_SEVERITY_META:
        return "error"
    return normalized


def _context_suffix(context: str | None) -> str:
    return f" for {context}" if context else ""


async def resolve_log_channel(
    bot: discord.Client,
    *,
    logger: Optional[logging.Logger] = None,
    context: str | None = None,
    raise_on_exception: bool = False,
) -> Optional[discord.abc.Messageable]:
    """Resolve the configured log channel, returning ``None`` on failure.

    When ``raise_on_exception`` is True, unexpected lookup failures will be re-raised.
    """
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


async def send_log_message(
    bot: discord.Client,
    *,
    content: str | None = None,
    embed: discord.Embed | None = None,
    allowed_mentions: discord.AllowedMentions | None = None,
    logger: Optional[logging.Logger] = None,
    context: str | None = None,
) -> bool:
    """Attempt to send a payload to the shared log channel."""
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


async def log_serious_issue(
    bot: discord.Client,
    *,
    summary: str,
    details: str | None = None,
    severity: str | None = None,
    context: str | None = None,
    logger: Optional[logging.Logger] = None,
) -> bool:
    """Send a standardized serious-issue log message to the configured log channel."""

    normalized_severity = _normalize_severity(severity)
    emoji, color = _SERIOUS_SEVERITY_META[normalized_severity]

    embed: discord.Embed | None = None
    if details:
        embed = discord.Embed(
            description=details,
            color=color,
        )
    success = await send_log_message(
        bot,
        content=f"{emoji} {summary}",
        embed=embed,
        allowed_mentions=discord.AllowedMentions.none(),
        logger=logger,
        context=context,
    )
    if not success:
        target_logger = logger or log
        target_logger.warning(
            "Failed to deliver serious issue log (%s): %s",
            normalized_severity,
            summary,
        )
    return success


__all__ = ["resolve_log_channel", "send_log_message", "log_serious_issue"]
