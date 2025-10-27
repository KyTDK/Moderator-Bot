from __future__ import annotations

import logging
from typing import Optional

import discord

from modules.nsfw_scanner.constants import LOG_CHANNEL_ID
from modules.utils.discord_utils import safe_get_channel

log = logging.getLogger(__name__)


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


__all__ = ["resolve_log_channel", "send_log_message"]
