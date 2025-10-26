from __future__ import annotations

import asyncio
import builtins
import logging
import os
from urllib.parse import urlparse

import aiohttp
import discord
import pillow_avif  # noqa: F401 - registers AVIF support

from modules.utils import clip_vectors
from modules.utils.discord_utils import safe_get_channel

from ..constants import ALLOWED_USER_IDS, LOG_CHANNEL_ID, TMP_DIR
from ..context import GuildScanContext, build_guild_scan_context
from .media_collector import collect_media_items, hydrate_message
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
            channel = await safe_get_channel(self.bot, LOG_CHANNEL_ID)
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

        if url:
            media_items.append(self._build_url_item(url))
        else:
            if target_message is None:
                return False
            target_message = await hydrate_message(target_message, bot=self.bot)
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
