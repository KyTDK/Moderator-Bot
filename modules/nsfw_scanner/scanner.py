from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any
from tempfile import NamedTemporaryFile
from urllib.parse import urlparse

import aiohttp
import discord
from discord.utils import escape_markdown, escape_mentions
from apnggif import apnggif
from cogs.hydration import wait_for_hydration
from discord.errors import NotFound
from discord.ext import commands
import pillow_avif  # registers AVIF support

from modules.config.premium_plans import PLAN_CORE, PLAN_FREE, PLAN_PRO, PLAN_ULTRA
from modules.nsfw_scanner.settings_keys import (
    NSFW_IMAGE_CATEGORY_SETTING,
    NSFW_TEXT_ACTION_SETTING,
    NSFW_TEXT_CATEGORY_SETTING,
    NSFW_TEXT_ENABLED_SETTING,
    NSFW_TEXT_SEND_EMBED_SETTING,
    NSFW_TEXT_STRIKES_ONLY_SETTING,
    NSFW_TEXT_THRESHOLD_SETTING,
    NSFW_THRESHOLD_SETTING,
)
from modules.utils import clip_vectors, mod_logging, mysql
from modules.utils.discord_utils import safe_get_channel
from modules.utils.log_channel import send_log_message

from .constants import (
    ACCELERATED_DOWNLOAD_CAP_BYTES,
    ACCELERATED_PRO_DOWNLOAD_CAP_BYTES,
    ACCELERATED_ULTRA_DOWNLOAD_CAP_BYTES,
    ALLOWED_USER_IDS,
    DEFAULT_DOWNLOAD_CAP_BYTES,
    LOG_CHANNEL_ID,
    NSFW_SCANNER_DEFAULT_HEADERS,
    TMP_DIR,
)
from .helpers import (
    AttachmentSettingsCache,
    check_attachment as helper_check_attachment,
    is_tenor_host,
    process_text,
    temp_download as helper_temp_download,
)
from .helpers.metrics import build_download_latency_breakdown
from .utils.file_ops import safe_delete

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
_tenor_toggle_cache: "OrderedDict[int, tuple[float, bool]]" = OrderedDict()


def _tenor_cache_get(guild_id: int) -> bool | None:
    entry = _tenor_toggle_cache.get(guild_id)
    if not entry:
        return None
    expires_at, value = entry
    if expires_at <= time.monotonic():
        _tenor_toggle_cache.pop(guild_id, None)
        return None
    refreshed_expiry = time.monotonic() + _TENOR_CACHE_TTL
    _tenor_toggle_cache[guild_id] = (refreshed_expiry, value)
    _tenor_toggle_cache.move_to_end(guild_id)
    return value


def _tenor_cache_set(guild_id: int, value: bool) -> None:
    expires_at = time.monotonic() + _TENOR_CACHE_TTL
    _tenor_toggle_cache[guild_id] = (expires_at, bool(value))
    _tenor_toggle_cache.move_to_end(guild_id)
    while len(_tenor_toggle_cache) > _TENOR_CACHE_MAX:
        _tenor_toggle_cache.popitem(last=False)


def _to_bool(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return bool(value)

class NSFWScanner:
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session = None
        self.tmp_dir = TMP_DIR
        self._clip_failure_callback_registered = False
        self._last_reported_milvus_error_key: str | None = None

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
    def _build_text_verbose_embed(
        *,
        author: discord.abc.User | None,
        channel: discord.abc.Messageable | None,
        guild_id: int | None,
        text_content: str,
        result: dict[str, Any] | None,
        message: discord.Message | None,
    ) -> discord.Embed:
        sanitized = escape_mentions(escape_markdown(text_content.strip()))
        snippet = sanitized[:512]
        if len(sanitized) > 512:
            snippet = sanitized[:509].rstrip() + "..."

        is_flagged = bool(result and result.get("is_nsfw"))
        decision_label = "Flagged" if is_flagged else "Allowed"
        color = discord.Color.red() if is_flagged else discord.Color.orange()

        lines: list[str] = []
        if author is not None:
            mention = getattr(author, "mention", None)
            if not mention:
                author_id = getattr(author, "id", None)
                mention = f"<@{author_id}>" if author_id else "Unknown user"
            lines.append(f"User: {mention}")
        if channel is not None:
            channel_name = getattr(channel, "mention", None) or getattr(channel, "name", None)
            channel_id = getattr(channel, "id", None)
            if channel_name and channel_id:
                lines.append(f"Channel: {channel_name} (`{channel_id}`)")
            elif channel_id is not None:
                lines.append(f"Channel ID: `{channel_id}`")
        if message is not None and getattr(message, "jump_url", None):
            lines.append(f"[Jump to message]({message.jump_url})")
        if guild_id is not None:
            lines.append(f"Guild ID: `{guild_id}`")
        lines.append(f"Decision: **{decision_label}**")

        embed = discord.Embed(
            title="NSFW Text Scan Report",
            description="\n".join(lines),
            color=color,
        )

        if snippet:
            code_block = snippet.replace("```", "`\u200b``")
            embed.add_field(
                name="Content Snippet",
                value=f"```{code_block}```",
                inline=False,
            )

        if isinstance(result, dict):
            category = result.get("category")
            if category:
                embed.add_field(name="Category", value=str(category), inline=True)
            reason = result.get("reason")
            if reason:
                embed.add_field(name="Reason", value=str(reason)[:1024], inline=True)
            score = result.get("score")
            if score is not None:
                try:
                    embed.add_field(name="Score", value=f"{float(score):.3f}", inline=True)
                except (TypeError, ValueError):
                    embed.add_field(name="Score", value=str(score), inline=True)
            similarity = result.get("similarity")
            if similarity is not None:
                try:
                    embed.add_field(name="Similarity", value=f"{float(similarity):.3f}", inline=True)
                except (TypeError, ValueError):
                    embed.add_field(name="Similarity", value=str(similarity), inline=True)
            threshold = result.get("threshold") or result.get("text_threshold")
            if threshold is not None:
                try:
                    embed.add_field(name="Threshold", value=f"{float(threshold):.3f}", inline=True)
                except (TypeError, ValueError):
                    embed.add_field(name="Threshold", value=str(threshold), inline=True)

        return embed

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
                value=self._truncate(f"{channel_name} (`{channel.id}`)", 1024),
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
    ) -> bool:

        settings_cache = AttachmentSettingsCache()
        latency_origin = overall_started_at

        async def _resolve_download_cap_bytes() -> int | None:
            if guild_id is None:
                return DEFAULT_DOWNLOAD_CAP_BYTES

            if settings_cache.has_premium_plan():
                plan = settings_cache.get_premium_plan()
            else:
                plan = None
                try:
                    plan = await mysql.resolve_guild_plan(guild_id)
                except Exception:
                    plan = None
                settings_cache.set_premium_plan(plan)

            normalized_plan = (plan or PLAN_FREE).lower()
            return _PLAN_DOWNLOAD_CAPS.get(normalized_plan, DEFAULT_DOWNLOAD_CAP_BYTES)

        download_cap_bytes = await _resolve_download_cap_bytes()

        async def _ensure_scan_settings_map() -> dict[str, Any]:
            settings = settings_cache.get_scan_settings()
            if settings is None and guild_id is not None:
                try:
                    settings = await mysql.get_settings(
                        guild_id,
                        [
                            NSFW_IMAGE_CATEGORY_SETTING,
                            NSFW_TEXT_CATEGORY_SETTING,
                            NSFW_THRESHOLD_SETTING,
                            NSFW_TEXT_THRESHOLD_SETTING,
                            NSFW_HIGH_ACCURACY_SETTING,
                            NSFW_TEXT_ENABLED_SETTING,
                            NSFW_TEXT_STRIKES_ONLY_SETTING,
                            NSFW_TEXT_SEND_EMBED_SETTING,
                        ],
                    )
                except Exception:
                    settings = None
                settings_cache.set_scan_settings(settings)
                settings = settings_cache.get_scan_settings()
            return settings or {}

        if url:
            return await self._download_and_scan(
                source_url=url,
                author=member,
                nsfw_callback=nsfw_callback,
                guild_id=guild_id,
                message=message,
                settings_cache=settings_cache,
                download_cap_bytes=download_cap_bytes,
                download_context="media from url",
                skip_context="media",
                skip_reason="download failure",
                propagate_value_error=True,
                overall_started_at=latency_origin,
            )
        snapshots = getattr(message, "message_snapshots", None)
        snapshot = snapshots[0] if snapshots else None

        attachments = message.attachments if message.attachments else (snapshot.attachments if snapshot else [])
        embeds = message.embeds if message.embeds else (snapshot.embeds if snapshot else [])
        stickers = message.stickers if message.stickers else (snapshot.stickers if snapshot else [])

        text_content = (message.content or "").strip()
        if text_content:
            text_scanning_enabled = False
            send_text_embed = True
            settings_map: dict[str, Any] | None = None
            if guild_id is not None:
                settings_map = await _ensure_scan_settings_map()

                text_enabled_value = settings_map.get(NSFW_TEXT_ENABLED_SETTING)
                text_scanning_enabled = _to_bool(text_enabled_value, default=False)
                settings_cache.set_text_enabled(text_scanning_enabled)

                if text_scanning_enabled:
                    if settings_cache.has_accelerated():
                        accelerated_allowed = bool(settings_cache.get_accelerated())
                    else:
                        try:
                            accelerated_allowed = await mysql.is_accelerated(guild_id=guild_id)
                        except Exception:
                            accelerated_allowed = False
                        settings_cache.set_accelerated(accelerated_allowed)
                    if not _to_bool(accelerated_allowed, default=False):
                        text_scanning_enabled = False

                if text_scanning_enabled:
                    strikes_only = _to_bool(
                        (settings_map or {}).get(NSFW_TEXT_STRIKES_ONLY_SETTING),
                        default=False,
                    )
                    if strikes_only:
                        author_id = getattr(getattr(message, "author", None), "id", None)
                        strike_count = 0
                        if author_id is not None:
                            try:
                                strike_count = await mysql.get_strike_count(author_id, guild_id)
                            except Exception:
                                strike_count = 0
                        if strike_count <= 0:
                            text_scanning_enabled = False

                    send_text_embed = _to_bool(
                        (settings_map or {}).get(NSFW_TEXT_SEND_EMBED_SETTING),
                        default=True,
                    )

            if text_scanning_enabled:
                author_id = getattr(getattr(message, "author", None), "id", None)
                text_metadata = {
                    "message_id": getattr(message, "id", None),
                    "channel_id": getattr(getattr(message, "channel", None), "id", None),
                    "author_id": author_id,
                }
                if author_id is not None:
                    text_metadata["user_id"] = author_id
                if guild_id is not None:
                    text_metadata["guild_id"] = guild_id
                text_result = await process_text(
                    self,
                    text_content,
                    guild_id=guild_id,
                    settings=settings_map,
                    payload_metadata=text_metadata,
                )
                log.debug(
                    "Text scan completed: guild_id=%s message_id=%s result=%s",
                    guild_id,
                    getattr(message, "id", None),
                    text_result,
                )

                verbose_enabled = False
                if message is not None and guild_id is not None:
                    if settings_cache.has_verbose():
                        verbose_enabled = bool(settings_cache.get_verbose())
                    else:
                        try:
                            verbose_enabled = bool(
                                await mysql.get_settings(guild_id, "nsfw-verbose")
                            )
                        except Exception:
                            verbose_enabled = False
                        settings_cache.set_verbose(verbose_enabled)

                verbose_embed: discord.Embed | None = None
                if text_result is not None and message is not None:
                    should_log = bool(text_result.get("is_nsfw")) or verbose_enabled
                    if should_log:
                        verbose_embed = self._build_text_verbose_embed(
                            author=getattr(message, "author", None),
                            channel=getattr(message, "channel", None),
                            guild_id=guild_id,
                            text_content=text_content,
                            result=text_result,
                            message=message,
                        )

                if verbose_enabled and verbose_embed is not None:
                    channel_obj = getattr(message, "channel", None)
                    channel_id = getattr(channel_obj, "id", None)
                    if channel_id is not None:
                        try:
                            await mod_logging.log_to_channel(
                                embed=verbose_embed,
                                channel_id=channel_id,
                                bot=self.bot,
                            )
                        except Exception as exc:
                            print(f"[verbose-text] Failed to send text verbose embed: {exc}")

                if verbose_embed is not None:
                    try:
                        log_embed = verbose_embed.copy()
                        log_embed.title = "NSFW Text Scan Debug"
                        await send_log_message(
                            self.bot,
                            embed=log_embed,
                            allowed_mentions=discord.AllowedMentions.none(),
                            context="nsfw_scanner.text_scan",
                        )
                    except Exception as exc:
                        log.debug("Failed to send text scan log: %s", exc, exc_info=True)

                if text_result and text_result.get("is_nsfw"):
                    if nsfw_callback:
                        category = text_result.get("category") or "unspecified"
                        confidence_value = None
                        confidence_source = None
                        score = text_result.get("score")
                        similarity = text_result.get("similarity")
                        try:
                            if score is not None:
                                confidence_value = float(score)
                                confidence_source = "score"
                            elif similarity is not None:
                                confidence_value = float(similarity)
                                confidence_source = "similarity"
                        except (TypeError, ValueError):
                            confidence_value = None
                            confidence_source = None

                        category_label = category.replace("_", " ").title()
                        reason = (
                            f"Detected potential policy violation (Category: **{category_label}**)."
                        )

                        await nsfw_callback(
                            message.author,
                            self.bot,
                            guild_id,
                            reason,
                            None,
                            message,
                            confidence=confidence_value,
                            confidence_source=confidence_source,
                            action_setting=NSFW_TEXT_ACTION_SETTING,
                            send_embed=send_text_embed,
                        )
                    return True

        # hydration fallback
        if not (attachments or embeds or stickers) and "http" in (message.content or ""):
            hydrated = await wait_for_hydration(message)
            if hydrated is not None:
                message = hydrated
            attachments = getattr(message, "attachments", None) or []
            embeds = getattr(message, "embeds", None) or []
            stickers = getattr(message, "stickers", None) or []

        if message is None:
            print("Message is None")
            return False

        for attachment in attachments:
            suffix = os.path.splitext(attachment.filename)[1] or ""
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
                            "duration_ms": (time.perf_counter() - save_started)
                            * 1000,
                            "label": "Attachment Save",
                        }
                    }
                except NotFound as exc:
                    safe_delete(tmp.name)
                    print(f"[NSFW] Attachment not found: {attachment.url}")
                    await self._report_download_failure(
                        source_url=attachment.url,
                        exc=exc,
                        message=message,
                    )
                    continue
                temp_filename = tmp.name
            try:
                start_point = latency_origin if latency_origin is not None else attachment_started_at
                flagged = await self._scan_local_file(
                    author=message.author,
                    temp_filename=temp_filename,
                    nsfw_callback=nsfw_callback,
                    guild_id=guild_id,
                    message=message,
                    settings_cache=settings_cache,
                    source=getattr(attachment, "url", attachment.filename),
                    log_context="attachment",
                    pre_latency_steps=pre_steps,
                    pre_download_bytes=pre_bytes,
                    overall_started_at=start_point,
                )
                latency_origin = None
                if flagged:
                    return True
            finally:
                safe_delete(temp_filename)

        for embed in embeds:
            possible_urls = []
            if embed.video and embed.video.url:
                possible_urls.append(embed.video.url)
            if embed.image and embed.image.url:
                possible_urls.append(embed.image.url)
            if embed.thumbnail and embed.thumbnail.url:
                possible_urls.append(embed.thumbnail.url)

            for gif_url in possible_urls:
                domain = urlparse(gif_url).netloc.lower()
                is_tenor = is_tenor_host(domain)
                if is_tenor:
                    check_tenor = True
                    if guild_id is not None:
                        cached_toggle = _tenor_cache_get(guild_id)
                        if cached_toggle is not None:
                            check_tenor = cached_toggle
                            settings_cache.set_check_tenor(check_tenor)
                        elif settings_cache.has_check_tenor():
                            check_tenor = bool(settings_cache.get_check_tenor())
                            _tenor_cache_set(guild_id, check_tenor)
                        else:
                            setting_value = await mysql.get_settings(
                                guild_id, "check-tenor-gifs"
                            )
                            check_tenor = bool(setting_value)
                            settings_cache.set_check_tenor(check_tenor)
                            _tenor_cache_set(guild_id, check_tenor)
                    if not check_tenor:
                        continue
                start_point = latency_origin
                flagged = await self._download_and_scan(
                    source_url=gif_url,
                    author=message.author,
                    nsfw_callback=nsfw_callback,
                    guild_id=guild_id,
                    message=message,
                    settings_cache=settings_cache,
                    download_cap_bytes=download_cap_bytes,
                    download_context="embedded media",
                    skip_context="media",
                    download_kwargs={"prefer_video": is_tenor},
                    overall_started_at=start_point,
                )
                latency_origin = None
                if flagged:
                    return True

        for sticker in stickers:
            sticker_url = sticker.url
            if not sticker_url:
                continue

            extension = sticker.format.name.lower()

            async def _sticker_postprocess(temp_path: str) -> tuple[str, list[str]]:
                if extension == "apng":
                    gif_location = os.path.join(TMP_DIR, f"{uuid.uuid4().hex[:12]}.gif")
                    await asyncio.to_thread(apnggif, temp_path, gif_location)
                    if not os.path.exists(gif_location):
                        log.warning(
                            "APNG conversion produced no output for %s; using original sticker payload",
                            temp_path,
                        )
                        return temp_path, []
                    return gif_location, [gif_location]
                return temp_path, []

            start_point = latency_origin
            flagged = await self._download_and_scan(
                source_url=sticker_url,
                author=message.author,
                nsfw_callback=nsfw_callback,
                guild_id=guild_id,
                message=message,
                settings_cache=settings_cache,
                download_cap_bytes=download_cap_bytes,
                download_context="sticker",
                download_kwargs={"ext": extension},
                postprocess=_sticker_postprocess,
                overall_started_at=start_point,
            )
            latency_origin = None
            if flagged:
                return True

        custom_emoji_tags = list(set(re.findall(r'<a?:\w+:\d+>', message.content)))
        for tag in custom_emoji_tags:
            match = re.match(r'<a?:(\w+):(\d+)>', tag)
            if not match:
                continue
            name, eid = match.groups()
            emoji_obj = self.bot.get_emoji(int(eid))
            if not emoji_obj:
                continue
            emoji_url = str(emoji_obj.url)
            start_point = latency_origin
            flagged = await self._download_and_scan(
                source_url=emoji_url,
                author=message.author,
                nsfw_callback=nsfw_callback,
                guild_id=guild_id,
                message=message,
                settings_cache=settings_cache,
                download_cap_bytes=download_cap_bytes,
                download_context="custom emoji",
                skip_context="emoji",
                propagate_download_exception=False,
                overall_started_at=start_point,
            )
            latency_origin = None
            if flagged:
                return True

        return False
