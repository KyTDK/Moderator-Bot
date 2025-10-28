from __future__ import annotations

import asyncio
import builtins
import logging
import os
from urllib.parse import urlparse

import aiohttp
import discord
import pillow_avif  # noqa: F401 - registers AVIF support
from discord.utils import utcnow

from modules.utils import clip_vectors
from modules.utils.log_channel import resolve_log_channel, send_log_message

from ..constants import ALLOWED_USER_IDS, LOG_CHANNEL_ID, TMP_DIR
from ..context import GuildScanContext, build_guild_scan_context
from ..utils.diagnostics import (
    DiagnosticRateLimiter,
    extract_context_lines,
    render_detail_lines,
    truncate_field_value,
)
from .media_collector import collect_media_items
from .media_worker import MediaFlagged, scan_media_item
from .work_item import MediaWorkItem

log = logging.getLogger(__name__)


class NSFWScanner:
    def __init__(self, bot: discord.Client):
        self.bot = bot
        self.session: aiohttp.ClientSession | None = None
        self.tmp_dir = TMP_DIR
        self._clip_failure_callback_registered = False
        self._last_reported_milvus_error_key: str | None = None
        self._diagnostic_limiter = DiagnosticRateLimiter()

    async def start(self):
        self.session = aiohttp.ClientSession()
        os.makedirs(self.tmp_dir, exist_ok=True)
        self._ensure_clip_failure_notifier()

    async def stop(self):
        if self.session:
            await self.session.close()

    def _ensure_clip_failure_notifier(self) -> None:
        if self._clip_failure_callback_registered:
            return
        clip_vectors.register_failure_callback(self._handle_milvus_failure)
        self._clip_failure_callback_registered = True

    async def _handle_milvus_failure(self, exc: Exception) -> None:
        error_key = f"{type(exc).__name__}:{exc}"
        if self._last_reported_milvus_error_key == error_key:
            return

        self._last_reported_milvus_error_key = error_key

        if not LOG_CHANNEL_ID:
            log.warning("Milvus failure detected but LOG_CHANNEL_ID is not configured")
            return

        mention = " ".join(f"<@{user_id}>" for user_id in ALLOWED_USER_IDS).strip()
        description = (
            "Failed to connect to Milvus at "
            f"{clip_vectors.MILVUS_HOST}:{clip_vectors.MILVUS_PORT}. "
            "Moderator Bot is falling back to the OpenAI `moderator_api` path until the vector index is available again."
        )
        embed = discord.Embed(
            title="Milvus connection failure",
            description=description,
            color=discord.Color.red(),
        )
        embed.add_field(
            name="Exception",
            value=f"`{type(exc).__name__}: {exc}`",
            inline=False,
        )
        embed.set_footer(text="OpenAI moderation fallback active")

        try:
            channel = await resolve_log_channel(
                self.bot,
                logger=log,
                context="milvus_failure",
                raise_on_exception=True,
            )
        except Exception as lookup_exc:
            log.warning(
                "Milvus failure detected but log channel %s could not be resolved: %s",
                LOG_CHANNEL_ID,
                lookup_exc,
            )
            return

        if channel is None:
            log.warning(
                "Milvus failure detected but log channel %s could not be found",
                LOG_CHANNEL_ID,
            )
            return

        try:
            await channel.send(
                content=mention or None,
                embed=embed,
            )
        except Exception as send_exc:
            log.warning(
                "Failed to report Milvus failure to channel %s: %s",
                LOG_CHANNEL_ID,
                send_exc,
            )
        else:
            log.warning(
                "Milvus failure reported to channel %s; OpenAI moderation fallback active",
                LOG_CHANNEL_ID,
            )

    async def is_nsfw(
        self,
        message: discord.Message | None = None,
        guild_id: int | None = None,
        nsfw_callback=None,
        url: str | None = None,
        member: discord.Member | None = None,
    ) -> bool:
        if self.session is None:
            raise RuntimeError("NSFWScanner session is not initialised. Call start() first.")

        resolved_guild_id = guild_id or getattr(getattr(message, "guild", None), "id", None)
        guild_context = await build_guild_scan_context(resolved_guild_id)

        media_items: list[MediaWorkItem] = []
        target_message = message

        if url and message is None:
            media_items.append(self._build_url_item(url))
        else:
            if target_message is None:
                return False
            media_items = collect_media_items(target_message, self.bot, guild_context)

        if not media_items:
            return False

        actor = member or getattr(target_message, "author", None)

        try:
            await self._fan_out_media(
                items=media_items,
                context=guild_context,
                message=target_message,
                actor=actor,
                nsfw_callback=nsfw_callback,
            )
        except MediaFlagged:
            return True
        except Exception as exc:  # noqa: BLE001
            base_group = getattr(builtins, "BaseExceptionGroup", None)
            if base_group is not None and isinstance(exc, base_group):
                matched, rest = exc.split(MediaFlagged)  # type: ignore[attr-defined]
                if matched is not None:
                    if rest is not None:
                        raise rest
                    return True
            raise
        return False

    async def _emit_collection_diagnostic(
        self,
        *,
        reason: str,
        guild_id: int | None,
        message: discord.Message | None,
        details: dict[str, object] | None = None,
    ) -> None:
        if not LOG_CHANNEL_ID:
            return
        throttle_key = f"{guild_id or 'global'}::{reason}"
        if not self._diagnostic_limiter.should_emit(throttle_key):
            return
        embed = discord.Embed(
            title="NSFW scan not started",
            color=discord.Color.orange(),
            timestamp=utcnow(),
        )
        embed.add_field(name="Reason", value=reason, inline=True)

        if guild_id is not None:
            embed.add_field(name="Guild", value=str(guild_id), inline=True)

        if message is not None:
            channel_id = getattr(getattr(message, "channel", None), "id", None)
            author = getattr(message, "author", None)

            embed.description = f"Message ID: {getattr(message, 'id', 'unknown')}"
            if author is not None:
                embed.add_field(name="Author", value=f"{getattr(author, 'id', 'unknown')}", inline=True)

            context_lines = extract_context_lines(
                message=message,
                include_author=False,
                include_message=False,
            )
            if context_lines:
                embed.add_field(
                    name="Context",
                    value="\n".join(context_lines),
                    inline=False,
                )
            content = getattr(message, "content", None)
            if content:
                preview = truncate_field_value(content.strip())
                if preview:
                    embed.add_field(name="Content Preview", value=preview, inline=False)

            if getattr(message, "jump_url", None):
                embed.add_field(
                    name="Message Link",
                    value=truncate_field_value(message.jump_url),
                    inline=False,
                )

        if details:
            detail_text = render_detail_lines(details)
            if detail_text:
                embed.add_field(name="Details", value=detail_text, inline=False)
        success = await send_log_message(
            self.bot,
            embed=embed,
            logger=log,
            context="nsfw_collection_diagnostic",
        )
        if not success:  # pragma: no cover - best effort logging
            log.debug(
                "Failed to send collection diagnostic to channel %s",
                LOG_CHANNEL_ID,
                exc_info=True,
            )

    async def _fan_out_media(
        self,
        *,
        items: list[MediaWorkItem],
        context: GuildScanContext,
        message: discord.Message | None,
        actor: discord.Member | None,
        nsfw_callback,
    ) -> None:
        async with asyncio.TaskGroup() as task_group:
            for item in items:
                task_group.create_task(
                    scan_media_item(
                        self,
                        item=item,
                        context=context,
                        message=message,
                        actor=actor,
                        nsfw_callback=nsfw_callback,
                    ),
                    name=f"nsfw:{item.source}:{item.label}",
                )

    def _build_url_item(self, url: str) -> MediaWorkItem:
        parsed = urlparse(url)
        ext = os.path.splitext(parsed.path)[1]
        return MediaWorkItem(
            source="url",
            label=url,
            url=url,
            ext_hint=ext or None,
        )


__all__ = ["NSFWScanner"]
