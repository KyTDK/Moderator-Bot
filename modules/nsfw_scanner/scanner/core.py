from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from collections.abc import Awaitable, Callable
from types import ModuleType
from typing import Any

import aiohttp
import discord
from discord.ext import commands

from modules.core.health import FeatureStatus, report_feature

try:
    import pillow_avif  # registers AVIF support
except ModuleNotFoundError:  # pragma: no cover - optional dependency
    pillow_avif = ModuleType("pillow_avif_stub")
    sys.modules.setdefault("pillow_avif", pillow_avif)
    logging.getLogger(__name__).warning(
        "pillow_avif is not installed; AVIF attachments may be rejected. Install "
        "\"pillow-avif-plugin\" to enable AVIF support."
    )
    report_feature(
        "media.avif",
        label="AVIF media decoding",
        status=FeatureStatus.DEGRADED,
        category="media",
        detail="pillow-avif-plugin missing; AVIF uploads rejected.",
        remedy="pip install pillow-avif-plugin",
        using_fallback=True,
    )
else:
    report_feature(
        "media.avif",
        label="AVIF media decoding",
        status=FeatureStatus.OK,
        category="media",
        detail="AVIF plugin active.",
    )

from cogs.hydration import wait_for_hydration
from modules.config.premium_plans import PLAN_CORE, PLAN_FREE, PLAN_PRO, PLAN_ULTRA
from modules.utils import clip_vectors, mod_logging, mysql

from ..constants import (
    ACCELERATED_DOWNLOAD_CAP_BYTES,
    ACCELERATED_PRO_DOWNLOAD_CAP_BYTES,
    ACCELERATED_ULTRA_DOWNLOAD_CAP_BYTES,
    DEFAULT_DOWNLOAD_CAP_BYTES,
    NSFW_SCANNER_DEFAULT_HEADERS,
    TMP_DIR,
)
from ..settings_keys import NSFW_TEXT_SOURCES_SETTING
from ..helpers import (
    AttachmentSettingsCache,
    check_attachment as helper_check_attachment,
    temp_download as helper_temp_download,
)
from ..helpers.metrics import build_download_latency_breakdown
from ..helpers.text_sources import (
    TEXT_SOURCE_MESSAGES,
    normalize_text_sources,
)
from ..tenor_cache import TenorToggleCache
from ..text_pipeline import TextScanPipeline
from ..utils.file_ops import safe_delete
from ..video_capabilities import evaluate_video_capabilities
from .contexts import MediaScanContext, ScanOutcome
from .failures import FailureReporter
from .media import MediaScanner
from .settings import resolve_download_cap_bytes, resolve_settings_map
from .utils import normalize_source_url, should_suppress_download_failure

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
    _normalize_source_url = staticmethod(normalize_source_url)
    _should_suppress_download_failure = staticmethod(should_suppress_download_failure)

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session = None
        self.tmp_dir = TMP_DIR
        self._clip_failure_callback_registered = False
        self._tenor_cache = TenorToggleCache(ttl=_TENOR_CACHE_TTL, max_items=_TENOR_CACHE_MAX)
        self._text_pipeline = TextScanPipeline(bot=bot)
        self._failure_reporter = FailureReporter(bot)

    async def start(self):
        session_headers = dict(NSFW_SCANNER_DEFAULT_HEADERS)
        self.session = aiohttp.ClientSession(headers=session_headers)
        os.makedirs(self.tmp_dir, exist_ok=True)
        self._ensure_clip_failure_notifier()
        evaluate_video_capabilities(source="nsfw_scanner.start")

    async def stop(self):
        if self.session:
            await self.session.close()

    def _ensure_clip_failure_notifier(self) -> None:
        if self._clip_failure_callback_registered:
            return
        clip_vectors.register_failure_callback(self._handle_milvus_failure)
        self._clip_failure_callback_registered = True

    async def _report_download_failure(
        self,
        *,
        source_url: str,
        exc: Exception,
        message: discord.Message | None = None,
    ) -> None:
        await self._failure_reporter.report_download_failure(
            source_url=source_url,
            exc=exc,
            message=message,
        )

    async def _report_scan_failure(
        self,
        *,
        source: str,
        exc: Exception,
        message: discord.Message | None = None,
    ) -> None:
        await self._failure_reporter.report_scan_failure(
            source=source,
            exc=exc,
            message=message,
        )

    async def _handle_milvus_failure(self, exc: Exception) -> None:
        await self._failure_reporter.report_milvus_failure(
            host=clip_vectors.MILVUS_HOST,
            port=clip_vectors.MILVUS_PORT,
            exc=exc,
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
        queue_label: str | None = None,
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
                queue_name=queue_label,
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
        queue_label: str | None = None,
    ) -> bool:
        normalized_url = self._normalize_source_url(source_url)
        if not normalized_url:
            log.debug(
                "Skipping %s %s due to empty URL after normalization",
                download_context,
                source_url,
            )
            return False
        source_url = normalized_url

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
                        queue_label=queue_label,
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
        queue_label: str | None = None,
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
            queue_label=queue_label,
        )
        media_scanner = MediaScanner(self, media_context)

        settings_map = await resolve_settings_map(guild_id, settings_cache)
        text_sources = normalize_text_sources(settings_map.get(NSFW_TEXT_SOURCES_SETTING))
        message_text_allowed = TEXT_SOURCE_MESSAGES in text_sources

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

        if scan_text and text_content and message_text_allowed:
            outcome.text_flagged = await self._text_pipeline.scan(
                scanner=self,
                message=message,
                guild_id=guild_id,
                nsfw_callback=nsfw_callback,
                settings_cache=settings_cache,
                settings_map=settings_map,
                queue_label=queue_label,
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
