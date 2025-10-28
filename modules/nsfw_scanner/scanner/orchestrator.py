from __future__ import annotations

import asyncio
import builtins
import logging
import os
from urllib.parse import urlparse

import aiohttp
import discord
import pillow_avif  # noqa: F401
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
from ..utils.file_types import ANIMATED_EXTS, VIDEO_EXTS

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
        clip_vectors.register_failure_callback(self._on_clip_failure)
        self._clip_failure_callback_registered = True

    async def _on_clip_failure(self, reason: str):
        if not reason or not LOG_CHANNEL_ID:
            return
        if reason == self._last_reported_milvus_error_key:
            return
        self._last_reported_milvus_error_key = reason
        await send_log_message(
            self.bot,
            f"⚠️ CLIP vector store issue: `{truncate_field_value(reason, 300)}`",
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
            media_items = await collect_media_items(target_message, self.bot, guild_context)

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
        except Exception as exc:
            base_group = getattr(builtins, "BaseExceptionGroup", None)
            if base_group is not None and isinstance(exc, base_group):
                matched, rest = exc.split(MediaFlagged)  # type: ignore[attr-defined]
                if matched is not None:
                    if rest is not None:
                        raise rest
                    return True

        return False

    def _build_url_item(self, url: str) -> MediaWorkItem:
        parsed = urlparse(str(url))
        ext = os.path.splitext(parsed.path)[1].lower()
        domain = parsed.netloc.lower()
        tenor = domain == "tenor.com" or domain.endswith(".tenor.com")
        prefer_video = tenor or ext in VIDEO_EXTS or ext in ANIMATED_EXTS
        return MediaWorkItem(
            source="url",
            label=str(url),
            url=str(url),
            prefer_video=prefer_video,
            ext_hint=ext or None,
            tenor=tenor,
            metadata={},
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
