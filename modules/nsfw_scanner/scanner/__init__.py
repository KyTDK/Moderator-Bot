from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp
import discord
from discord.errors import NotFound
from discord.ext import commands
import pillow_avif  # registers AVIF support

from cogs.hydration import wait_for_hydration
from modules.config.premium_plans import PLAN_CORE, PLAN_FREE, PLAN_PRO, PLAN_ULTRA
from modules.utils import clip_vectors, mod_logging, mysql
from modules.utils.discord_utils import safe_get_channel
from modules.utils.log_channel import send_log_message

from ..constants import (
    ACCELERATED_DOWNLOAD_CAP_BYTES,
    ACCELERATED_PRO_DOWNLOAD_CAP_BYTES,
    ACCELERATED_ULTRA_DOWNLOAD_CAP_BYTES,
    ALLOWED_USER_IDS,
    DEFAULT_DOWNLOAD_CAP_BYTES,
    LOG_CHANNEL_ID,
    NSFW_SCANNER_DEFAULT_HEADERS,
    TMP_DIR,
)
from ..helpers import (
    AttachmentSettingsCache,
    check_attachment as helper_check_attachment,
    temp_download as helper_temp_download,
)
from ..helpers.metrics import build_download_latency_breakdown
from ..tenor_cache import TenorToggleCache
from ..text_pipeline import TextScanPipeline
from ..utils.file_ops import safe_delete
from .contexts import MediaScanContext, ScanOutcome
from .media import MediaScanner
from .settings import resolve_download_cap_bytes, resolve_settings_map

__all__ = ["NSFWScanner", "wait_for_hydration"]

log = logging.getLogger(__name__)

_PLAN_DOWNLOAD_CAPS: dict[str, int | None] = {
    PLAN_CORE: ACCELERATED_DOWNLOAD_CAP_BYTES,
    PLAN_PRO: ACCELERATED_PRO_DOWNLOAD_CAP_BYTES,
    PLAN_ULTRA: ACCELERATED_ULTRA_DOWNLOAD_CAP_BYTES,
    "accelerated": ACCELERATED_DOWNLOAD_CAP_BYTES,
    "accelerated_core": ACCELERATED_DOWNLOAD_CAP_BYTES,
    "accelerated_pro": ACCELERATED_PRO_DOWNLOAD_CAP_BYTES,
    "accelerated_ultra": ACCELERATED_ULTRA_DOWNLOAD_CAP_BYTES,
}

_TENOR_CACHE_TTL = 600.0
_TENOR_CACHE_MAX = 512


class NSFWScanner:
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session = None
        self.tmp_dir = TMP_DIR
        self._clip_failure_callback_registered = False
        self._last_reported_milvus_error_key: str | None = None
        self._tenor_cache = TenorToggleCache(ttl=_TENOR_CACHE_TTL, max_items=_TENOR_CACHE_MAX)
        self._text_pipeline = TextScanPipeline(bot=bot)

    async def start(self):
        session_headers = dict(NSFW_SCANNER_DEFAULT_HEADERS)
        self.session = aiohttp.ClientSession(headers=session_headers)
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

    @staticmethod
    def _truncate(value: str, limit: int = 1024) -> str:
        if len(value) <= limit:
            return value
        return value[: limit - 1] + "\u2026"

    @staticmethod
    def _should_suppress_download_failure(exc: Exception) -> bool:
        if isinstance(exc, aiohttp.ClientResponseError):
            status = getattr(exc, "status", None)
            return status in {404, 410, 451}
        if isinstance(exc, (FileNotFoundError, NotFound)):
            return True
        return False

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

        description = self._truncate(f"Source: {source}", 2048)
        embed = discord.Embed(title=title, description=description, color=discord.Color.red())

        error_value = self._truncate(f"`{type(exc).__name__}: {exc}`")
        embed.add_field(name="Error", value=error_value or "(no details)", inline=False)

        if message is not None and getattr(message, "jump_url", None):
            embed.add_field(
                name="Message",
                value=f"[Jump to message]({message.jump_url})",
                inline=False,
            )

        guild = getattr(message, "guild", None)
        if guild is not None:
            embed.add_field(
                name="Guild",
                value=self._truncate(f"{getattr(guild, 'name', 'Unknown')} (`{guild.id}`)", 1024),
                inline=False,
            )

        channel = getattr(message, "channel", None)
        if channel is not None and getattr(channel, "id", None):
            channel_name = getattr(channel, "name", None) or getattr(channel, "id", "Unknown")
            embed.add_field(
                name="Channel",
                value=self._truncate(f"{channel_name}", 1024),
                inline=False,
            )

        success = await send_log_message(
            self.bot,
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
            context=context,
        )
        if not success:
            log.debug("Failed to report %s to LOG_CHANNEL_ID=%s", context, LOG_CHANNEL_ID)

    async def _report_download_failure(
        self,
        *,
        source_url: str,
        exc: Exception,
        message: discord.Message | None = None,
    ) -> None:
        if self._should_suppress_download_failure(exc):
            log.debug(
                "Suppressed download failure for %s (%s)",
                source_url,
                exc,
            )
            return
        await self._send_failure_log(
            title="Download failure",
            source=source_url,
            exc=exc,
            message=message,
            context="nsfw_scanner.download",
        )

    async def _report_scan_failure(
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

    async def _scan_local_file(
        self,
        *,
        author: discord.abc.User | None,
        temp_filename: str,
        nsfw_callback,
        guild_id: int | None,
        message: discord.Message | None,
        settings_cache: AttachmentSettingsCache,
        source: str,
        log_context: str,
        pre_latency_steps: dict[str, dict[str, Any]] | None = None,
        pre_download_bytes: int | None = None,
        overall_started_at: float | None = None,
    ) -> bool:
        try:
            return await helper_check_attachment(
                self,
                author,
                temp_filename,
                nsfw_callback,
                guild_id,
                message,
                settings_cache=settings_cache,
                pre_latency_steps=pre_latency_steps,
                pre_download_bytes=pre_download_bytes,
                source_url=source,
                overall_started_at=overall_started_at,
            )
        except Exception as scan_exc:
            log.exception("Failed to scan %s %s", log_context, source)
            await self._report_scan_failure(
                source=source,
                exc=scan_exc,
                message=message,
            )
            raise

    async def _download_and_scan(
        self,
        *,
        source_url: str,
        author: discord.abc.User | None,
        nsfw_callback,
        guild_id: int | None,
        message: discord.Message | None,
        settings_cache: AttachmentSettingsCache,
        download_cap_bytes: int | None,
        download_context: str,
        skip_context: str | None = None,
        skip_reason: str = "download cap",
        download_kwargs: dict[str, Any] | None = None,
        postprocess: Callable[[str], Awaitable[tuple[str, list[str]]]] | None = None,
        propagate_value_error: bool = False,
        propagate_download_exception: bool = True,
        overall_started_at: float | None = None,
    ) -> bool:
        download_kwargs = download_kwargs or {}
        skip_context = skip_context or download_context
        scan_failed = False
        if overall_started_at is None:
            overall_started_at = time.perf_counter()
        try:
            async with helper_temp_download(
                self.session,
                source_url,
                download_cap_bytes=download_cap_bytes,
                **download_kwargs,
            ) as download_result:
                processed_path = download_result.path
                cleanup_paths: list[str] = []
                pre_latency_steps = build_download_latency_breakdown(
                    download_result.telemetry
                )
                pre_download_bytes = download_result.telemetry.bytes_downloaded
                try:
                    if postprocess is not None:
                        processed_path, extra_paths = await postprocess(
                            download_result.path
                        )
                        if isinstance(extra_paths, str):
                            cleanup_paths = [extra_paths]
                        else:
                            cleanup_paths = list(extra_paths)
                    return await self._scan_local_file(
                        author=author,
                        temp_filename=processed_path,
                        nsfw_callback=nsfw_callback,
                        guild_id=guild_id,
                        message=message,
                        settings_cache=settings_cache,
                        source=source_url,
                        log_context=download_context,
                        pre_latency_steps=pre_latency_steps,
                        pre_download_bytes=pre_download_bytes,
                        overall_started_at=overall_started_at,
                    )
                except Exception:
                    scan_failed = True
                    raise
                finally:
                    for path in cleanup_paths:
                        safe_delete(path)
        except ValueError as download_error:
            log.debug(
                "Skipping %s %s due to %s: %s",
                skip_context,
                source_url,
                skip_reason,
                download_error,
            )
            await self._report_download_failure(
                source_url=source_url,
                exc=download_error,
                message=message,
            )
            if propagate_value_error:
                raise
            return False
        except Exception as download_exc:
            if scan_failed:
                raise
            suppress_download = self._should_suppress_download_failure(download_exc)
            if suppress_download:
                log.debug(
                    "Skipping %s %s due to %s",
                    download_context,
                    source_url,
                    download_exc,
                )
            else:
                log.exception("Failed to download %s %s", download_context, source_url)
            await self._report_download_failure(
                source_url=source_url,
                exc=download_exc,
                message=message,
            )
            if propagate_download_exception and not suppress_download:
                raise
            return False

        return False

    async def is_nsfw(
        self,
        message: discord.Message | None = None,
        guild_id: int | None = None,
        nsfw_callback=None,
        url: str | None = None,
        member: discord.Member | None = None,
        overall_started_at: float | None = None,
        *,
        scan_text: bool = True,
        scan_media: bool = True,
        return_details: bool = False,
    ):
        settings_cache = AttachmentSettingsCache()
        outcome = ScanOutcome()

        download_cap_bytes = await resolve_download_cap_bytes(
            guild_id,
            settings_cache,
            _PLAN_DOWNLOAD_CAPS,
            DEFAULT_DOWNLOAD_CAP_BYTES,
        )
        media_context = MediaScanContext(
            message=message,
            guild_id=guild_id,
            nsfw_callback=nsfw_callback,
            settings_cache=settings_cache,
            download_cap_bytes=download_cap_bytes,
            author=member or (getattr(message, "author", None) if message else None),
            latency_origin=overall_started_at,
        )
        media_scanner = MediaScanner(self, media_context)

        settings_map = await resolve_settings_map(guild_id, settings_cache)

        if url:
            outcome.media_flagged = await media_scanner.scan_remote_media(
                url=url,
                download_context="media from url",
                skip_context="media",
                skip_reason="download failure",
                propagate_value_error=True,
            )
            return outcome.packed(return_details)

        if message is None:
            return outcome.packed(return_details)

        text_content = (message.content or "").strip()

        if scan_text and text_content:
            outcome.text_flagged = await self._text_pipeline.scan(
                scanner=self,
                message=message,
                guild_id=guild_id,
                nsfw_callback=nsfw_callback,
                settings_cache=settings_cache,
                settings_map=settings_map,
            )
            if outcome.text_flagged:
                return outcome.packed(return_details)

        if not scan_media:
            return outcome.packed(return_details)

        snapshots = getattr(message, "message_snapshots", None)
        snapshot = snapshots[0] if snapshots else None

        message, attachments, embeds, stickers = await media_scanner.gather_targets(
            message,
            snapshot,
            text_content,
        )
        if message is None:
            return outcome.packed(return_details)

        media_context.update_message(message)

        if attachments and await media_scanner.scan_attachments(attachments):
            outcome.media_flagged = True
            return outcome.packed(return_details)

        if embeds and await media_scanner.scan_embeds(embeds):
            outcome.media_flagged = True
            return outcome.packed(return_details)

        if stickers and await media_scanner.scan_stickers(stickers):
            outcome.media_flagged = True
            return outcome.packed(return_details)

        if await media_scanner.scan_custom_emojis(getattr(message, "content", "") or ""):
            outcome.media_flagged = True
            return outcome.packed(return_details)

        return outcome.packed(return_details)

    async def _resolve_tenor_setting(self, guild_id: int):
        return await mysql.get_settings(guild_id, "check-tenor-gifs")
