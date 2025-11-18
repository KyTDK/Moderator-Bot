from __future__ import annotations

import logging

import discord

from modules.nsfw_scanner.constants import ALLOWED_USER_IDS, LOG_CHANNEL_ID
from modules.utils.log_channel import DeveloperLogField, log_to_developer_channel

from .utils import should_suppress_download_failure, truncate

log = logging.getLogger(__name__)


class FailureReporter:
    """Centralized helper for NSFW scanner failure logging."""

    def __init__(self, bot: discord.Client):
        self._bot = bot
        self._last_reported_milvus_error_key: str | None = None

    async def report_download_failure(
        self,
        *,
        source_url: str,
        exc: Exception,
        message: discord.Message | None = None,
    ) -> None:
        if should_suppress_download_failure(exc):
            log.debug("Suppressed download failure for %s (%s)", source_url, exc)
            return
        await self._send_failure_log(
            title="Download failure",
            source=source_url,
            exc=exc,
            message=message,
            context="nsfw_scanner.download",
        )

    async def report_scan_failure(
        self,
        *,
        source: str,
        exc: Exception,
        message: discord.Message | None = None,
    ) -> None:
        await self._send_failure_log(
            title="NSFW scan failure",
            source=source,
            exc=exc,
            message=message,
            context="nsfw_scanner.scan",
        )

    async def report_milvus_failure(
        self,
        *,
        host: str,
        port: int,
        exc: Exception,
    ) -> None:
        error_key = f"{type(exc).__name__}:{exc}"
        if self._last_reported_milvus_error_key == error_key:
            return
        self._last_reported_milvus_error_key = error_key

        if not LOG_CHANNEL_ID:
            log.warning("Milvus failure detected but LOG_CHANNEL_ID is not configured")
            return

        mention = " ".join(f"<@{user_id}>" for user_id in ALLOWED_USER_IDS).strip() or None
        description = (
            "Failed to connect to Milvus at "
            f"{host}:{port}. "
            "Moderator Bot is falling back to the OpenAI `moderator_api` path until the vector index is available again."
        )
        embed_fields = [
            DeveloperLogField(
                name="Exception",
                value=f"`{type(exc).__name__}: {exc}`",
                inline=False,
            )
        ]

        try:
            success = await log_to_developer_channel(
                self._bot,
                summary="Milvus connection failure",
                severity="error",
                description=description,
                fields=embed_fields,
                mention=mention,
                footer="OpenAI moderation fallback active",
                context="nsfw_scanner.milvus_failure",
            )
        except Exception as send_exc:
            log.warning(
                "Failed to report Milvus failure to channel %s: %s",
                LOG_CHANNEL_ID,
                send_exc,
            )
            return

        if success:
            log.warning(
                "Milvus failure reported to channel %s; OpenAI moderation fallback active",
                LOG_CHANNEL_ID,
            )
        else:
            log.warning(
                "Milvus failure detected but no message was delivered to LOG_CHANNEL_ID=%s",
                LOG_CHANNEL_ID,
            )

    async def _send_failure_log(
        self,
        *,
        title: str,
        source: str,
        exc: Exception,
        message: discord.Message | None,
        context: str,
    ) -> None:
        if not LOG_CHANNEL_ID:
            return

        description = truncate(f"Source: {source}", 2048)
        error_value = truncate(f"`{type(exc).__name__}: {exc}`")

        fields: list[DeveloperLogField] = [
            DeveloperLogField(name="Error", value=error_value or "(no details)", inline=False),
        ]

        if message is not None and getattr(message, "jump_url", None):
            fields.append(
                DeveloperLogField(
                    name="Message",
                    value=f"[Jump to message]({message.jump_url})",
                    inline=False,
                )
            )

        guild = getattr(message, "guild", None)
        if guild is not None:
            guild_value = truncate(
                f"{getattr(guild, 'name', 'Unknown')} (`{getattr(guild, 'id', 'unknown')}`)",
                1024,
            )
            fields.append(DeveloperLogField(name="Guild", value=guild_value, inline=False))

        channel = getattr(message, "channel", None)
        if channel is not None and getattr(channel, "id", None):
            channel_name = getattr(channel, "name", None) or getattr(channel, "id", "Unknown")
            fields.append(
                DeveloperLogField(
                    name="Channel",
                    value=truncate(f"{channel_name}", 1024),
                    inline=False,
                )
            )

        success = await log_to_developer_channel(
            self._bot,
            summary=title,
            severity="error",
            description=description,
            fields=fields,
            context=context,
        )
        if not success:
            log.debug("Failed to report %s to LOG_CHANNEL_ID=%s", context, LOG_CHANNEL_ID)


__all__ = ["FailureReporter"]
