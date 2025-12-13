from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import uuid
from tempfile import NamedTemporaryFile
from typing import Any, Iterable
from urllib.parse import urlparse

from discord.errors import NotFound
try:
    from apnggif import apnggif as _apnggif  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - optional dependency
    _apnggif = None  # type: ignore

from cogs.hydration import wait_for_hydration

from ..constants import TMP_DIR
from ..helpers import is_tenor_host
from ..helpers.attachments import AttachmentSettingsCache
from ..utils.file_ops import safe_delete
from .contexts import MediaScanContext

if False:  # pragma: no cover - only used for typing
    from . import NSFWScanner

log = logging.getLogger(__name__)


class MediaScanner:
    def __init__(self, scanner: "NSFWScanner", context: MediaScanContext):
        self._scanner = scanner
        self._context = context

    async def gather_targets(
        self,
        message,
        snapshot,
        text_content: str,
    ) -> tuple[Any, list[Any], list[Any], list[Any]]:
        attachments, embeds, stickers = self._extract_collections(message)
        if not (attachments or embeds or stickers) and snapshot is not None:
            snap_attachments, snap_embeds, snap_stickers = self._extract_collections(snapshot)
            if not attachments:
                attachments = snap_attachments
            if not embeds:
                embeds = snap_embeds
            if not stickers:
                stickers = snap_stickers

        if not (attachments or embeds or stickers) and message is not None and "http" in text_content:
            hydrated = await wait_for_hydration(message)
            if hydrated is not None:
                message = hydrated
                self._context.update_message(hydrated)
                attachments, embeds, stickers = self._extract_collections(hydrated)

        return message, attachments, embeds, stickers

    async def scan_attachments(self, attachments: Iterable[Any]) -> bool:
        for attachment in attachments:
            if await self._scan_attachment(attachment):
                return True
        return False

    async def scan_embeds(self, embeds: Iterable[Any]) -> bool:
        tenor_allowed: bool | None = None
        for embed in embeds:
            for media_url in self._iter_embed_urls(embed):
                domain = urlparse(media_url).netloc.lower()
                is_tenor = is_tenor_host(domain)
                if is_tenor:
                    if tenor_allowed is None:
                        tenor_allowed = await self._tenor_enabled()
                    if not tenor_allowed:
                        continue
                download_kwargs = {"prefer_video": True} if is_tenor else None
                if await self._scan_remote_media(
                    url=media_url,
                    download_context="embedded media",
                    skip_context="media",
                    download_kwargs=download_kwargs,
                ):
                    return True
        return False

    async def scan_stickers(self, stickers: Iterable[Any]) -> bool:
        for sticker in stickers:
            sticker_url = getattr(sticker, "url", None)
            if not sticker_url:
                continue
            sticker_format = getattr(sticker, "format", None)
            extension = ""
            if sticker_format is not None:
                extension = (getattr(sticker_format, "name", "") or "").lower()

            async def _sticker_postprocess(temp_path: str) -> tuple[str, list[str]]:
                if extension == "apng":
                    if _apnggif is None:
                        log.warning(
                            "APNG sticker received but 'apnggif' is not installed; using original APNG payload"
                        )
                        return temp_path, []
                    gif_location = os.path.join(TMP_DIR, f"{uuid.uuid4().hex[:12]}.gif")
                    await asyncio.to_thread(_apnggif, temp_path, gif_location)
                    if not os.path.exists(gif_location):
                        log.warning(
                            "APNG conversion produced no output for %s; using original sticker payload",
                            temp_path,
                        )
                        return temp_path, []
                    return gif_location, [gif_location]
                return temp_path, []

            if await self._scan_remote_media(
                url=sticker_url,
                download_context="sticker",
                download_kwargs={"ext": extension} if extension else None,
                postprocess=_sticker_postprocess,
            ):
                return True
        return False

    async def scan_custom_emojis(self, content: str) -> bool:
        if not content:
            return False

        seen: set[str] = set()
        for match in re.finditer(r'<a?:\w+:\d+>', content):
            tag = match.group(0)
            if tag in seen:
                continue
            seen.add(tag)
            matcher = re.match(r'<a?:(\w+):(\d+)>', tag)
            if not matcher:
                continue
            _, emoji_id = matcher.groups()
            emoji_obj = self._scanner.bot.get_emoji(int(emoji_id))
            if not emoji_obj:
                continue
            if await self._scan_remote_media(
                url=str(emoji_obj.url),
                download_context="custom emoji",
                skip_context="emoji",
                propagate_download_exception=False,
            ):
                return True
        return False

    async def scan_remote_media(
        self,
        *,
        url: str,
        download_context: str,
        skip_context: str = "media",
        skip_reason: str = "download cap",
        download_kwargs: dict[str, Any] | None = None,
        postprocess=None,
        propagate_download_exception: bool = True,
        propagate_value_error: bool = False,
    ) -> bool:
        return await self._scan_remote_media(
            url=url,
            download_context=download_context,
            skip_context=skip_context,
            skip_reason=skip_reason,
            download_kwargs=download_kwargs,
            postprocess=postprocess,
            propagate_download_exception=propagate_download_exception,
            propagate_value_error=propagate_value_error,
        )

    async def _scan_attachment(self, attachment) -> bool:
        suffix = os.path.splitext(getattr(attachment, "filename", "") or "")[1] or ""
        pre_steps: dict[str, dict[str, Any]] | None = None
        pre_bytes = getattr(attachment, "size", None)
        attachment_started_at: float | None = None
        with NamedTemporaryFile(delete=False, dir=TMP_DIR, suffix=suffix) as tmp:
            try:
                save_started = time.perf_counter()
                attachment_started_at = save_started
                await attachment.save(tmp.name)
                pre_steps = {
                    "download_attachment_save": {
                        "duration_ms": (time.perf_counter() - save_started) * 1000,
                        "label": "Attachment Save",
                    }
                }
            except asyncio.TimeoutError as exc:
                safe_delete(tmp.name)
                log.warning(
                    "[NSFW] Attachment download timed out for %s", getattr(attachment, "url", "unknown")
                )
                await self._scanner._report_download_failure(
                    source_url=getattr(attachment, "url", None) or getattr(attachment, "filename", "unknown"),
                    exc=exc,
                    message=self._context.message,
                )
                return False
            except NotFound as exc:
                safe_delete(tmp.name)
                print(f"[NSFW] Attachment not found: {getattr(attachment, 'url', 'unknown')}")
                await self._scanner._report_download_failure(
                    source_url=getattr(attachment, "url", None) or getattr(attachment, "filename", "unknown"),
                    exc=exc,
                    message=self._context.message,
                )
                return False
            temp_filename = tmp.name
        try:
            overall_started_at = self._context.consume_latency_origin()
            if overall_started_at is None:
                overall_started_at = attachment_started_at
            return await self._scanner._scan_local_file(
                author=self._context.author,
                temp_filename=temp_filename,
                nsfw_callback=self._context.nsfw_callback,
                guild_id=self._context.guild_id,
                message=self._context.message,
                settings_cache=self._context.settings_cache,
                source=getattr(attachment, "url", None) or getattr(attachment, "filename", "unknown"),
                log_context="attachment",
                pre_latency_steps=pre_steps,
                pre_download_bytes=pre_bytes,
                overall_started_at=overall_started_at,
                queue_label=self._context.queue_label,
            )
        finally:
            safe_delete(temp_filename)

    async def _scan_remote_media(
        self,
        *,
        url: str,
        download_context: str,
        skip_context: str = "media",
        skip_reason: str = "download cap",
        download_kwargs: dict[str, Any] | None = None,
        postprocess=None,
        propagate_download_exception: bool = True,
        propagate_value_error: bool = False,
    ) -> bool:
        return await self._scanner._download_and_scan(
            source_url=url,
            author=self._context.author,
            nsfw_callback=self._context.nsfw_callback,
            guild_id=self._context.guild_id,
            message=self._context.message,
            settings_cache=self._context.settings_cache,
            download_cap_bytes=self._context.download_cap_bytes,
            download_context=download_context,
            skip_context=skip_context,
            skip_reason=skip_reason,
            download_kwargs=download_kwargs,
            postprocess=postprocess,
            propagate_value_error=propagate_value_error,
            propagate_download_exception=propagate_download_exception,
            overall_started_at=self._context.consume_latency_origin(),
            queue_label=self._context.queue_label,
        )

    async def _tenor_enabled(self) -> bool:
        guild_id = self._context.guild_id
        if guild_id is None:
            return True

        cached_toggle = self._scanner._tenor_cache.get(guild_id)
        if cached_toggle is not None:
            self._context.settings_cache.set_check_tenor(cached_toggle)
            return cached_toggle

        settings_cache: AttachmentSettingsCache = self._context.settings_cache
        if settings_cache.has_check_tenor():
            value = bool(settings_cache.get_check_tenor())
            self._scanner._tenor_cache.set(guild_id, value)
            return value

        try:
            setting_value = await self._scanner._resolve_tenor_setting(guild_id)
        except Exception:
            setting_value = None
        value = bool(setting_value)
        settings_cache.set_check_tenor(value)
        self._scanner._tenor_cache.set(guild_id, value)
        return value

    @staticmethod
    def _iter_embed_urls(embed) -> list[str]:
        urls: list[str] = []
        video = getattr(embed, "video", None)
        if getattr(video, "url", None):
            urls.append(video.url)
        image = getattr(embed, "image", None)
        if getattr(image, "url", None):
            urls.append(image.url)
        thumb = getattr(embed, "thumbnail", None)
        if getattr(thumb, "url", None):
            urls.append(thumb.url)
        return urls

    @staticmethod
    def _extract_collections(source) -> tuple[list[Any], list[Any], list[Any]]:
        if source is None:
            return [], [], []
        attachments = list(getattr(source, "attachments", []) or [])
        embeds = list(getattr(source, "embeds", []) or [])
        stickers = list(getattr(source, "stickers", []) or [])
        return attachments, embeds, stickers
