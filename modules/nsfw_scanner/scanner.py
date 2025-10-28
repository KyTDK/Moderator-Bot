import asyncio
import logging
import os
import re
import time
import uuid
from collections import OrderedDict
from tempfile import NamedTemporaryFile
from urllib.parse import urlparse

import aiohttp
import discord
from apnggif import apnggif
from cogs.hydration import wait_for_hydration
from discord.errors import NotFound
from discord.ext import commands
import pillow_avif  # registers AVIF support

from modules.config.premium_plans import PLAN_CORE, PLAN_FREE, PLAN_PRO, PLAN_ULTRA
from modules.utils import clip_vectors, mysql
from modules.utils.discord_utils import safe_get_channel

from .constants import (
    ACCELERATED_DOWNLOAD_CAP_BYTES,
    ACCELERATED_PRO_DOWNLOAD_CAP_BYTES,
    ACCELERATED_ULTRA_DOWNLOAD_CAP_BYTES,
    ALLOWED_USER_IDS,
    DEFAULT_DOWNLOAD_CAP_BYTES,
    LOG_CHANNEL_ID,
    TMP_DIR,
)
from .helpers import (
    AttachmentSettingsCache,
    check_attachment as helper_check_attachment,
    temp_download as helper_temp_download,
)
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

class NSFWScanner:
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session = None
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

        settings_cache = AttachmentSettingsCache()

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

        if url:
            async with helper_temp_download(
                self.session, url, download_cap_bytes=download_cap_bytes
            ) as temp_filename:
                return await helper_check_attachment(
                    self,
                    member,
                    temp_filename,
                    nsfw_callback,
                    guild_id,
                    message,
                    settings_cache=settings_cache,
                )
        snapshots = getattr(message, "message_snapshots", None)
        snapshot = snapshots[0] if snapshots else None

        attachments = message.attachments if message.attachments else (snapshot.attachments if snapshot else [])
        embeds = message.embeds if message.embeds else (snapshot.embeds if snapshot else [])
        stickers = message.stickers if message.stickers else (snapshot.stickers if snapshot else [])

        # hydration fallback
        if not (attachments or embeds or stickers) and "http" in message.content:
            message = await wait_for_hydration(message)
            attachments = message.attachments
            embeds = message.embeds
            stickers = message.stickers

            if not (attachments or embeds or stickers):
                return False

        if message is None:
            print("Message is None")
            return False

        for attachment in attachments:
            suffix = os.path.splitext(attachment.filename)[1] or ""
            with NamedTemporaryFile(delete=False, dir=TMP_DIR, suffix=suffix) as tmp:
                try:
                    await attachment.save(tmp.name)
                except NotFound:
                    safe_delete(tmp.name)
                    print(f"[NSFW] Attachment not found: {attachment.url}")
                    continue
                temp_filename = tmp.name
            try:
                if await helper_check_attachment(
                    self,
                    message.author,
                    temp_filename,
                    nsfw_callback,
                    guild_id,
                    message,
                    settings_cache=settings_cache,
                ):
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
                is_tenor = domain == "tenor.com" or domain.endswith(".tenor.com")
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
                try:
                    async with helper_temp_download(
                        self.session,
                        gif_url,
                        prefer_video=is_tenor,
                        download_cap_bytes=download_cap_bytes,
                    ) as temp_filename:
                        if await helper_check_attachment(
                            self,
                            author=message.author,
                            temp_filename=temp_filename,
                            nsfw_callback=nsfw_callback,
                            guild_id=guild_id,
                            message=message,
                            settings_cache=settings_cache,
                        ):
                            return True
                except ValueError as download_error:
                    log.debug(
                        "Skipping media %s due to download cap: %s",
                        gif_url,
                        download_error,
                    )

        for sticker in stickers:
            sticker_url = sticker.url
            if not sticker_url:
                continue

            extension = sticker.format.name.lower()

            try:
                async with helper_temp_download(
                    self.session,
                    sticker_url,
                    ext=extension,
                    download_cap_bytes=download_cap_bytes,
                ) as temp_location:
                    gif_location = temp_location

                    if extension == "apng":
                        gif_location = os.path.join(TMP_DIR, f"{uuid.uuid4().hex[:12]}.gif")
                        await asyncio.to_thread(apnggif, temp_location, gif_location)

                    try:
                        if await helper_check_attachment(
                            self,
                            message.author,
                            gif_location,
                            nsfw_callback,
                            guild_id,
                            message,
                            settings_cache=settings_cache,
                        ):
                            return True
                    finally:
                        if gif_location != temp_location:
                            safe_delete(gif_location)
            except ValueError as download_error:
                log.debug(
                    "Skipping sticker %s due to download cap: %s",
                    sticker_url,
                    download_error,
                )

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
            try:
                async with helper_temp_download(
                    self.session,
                    emoji_url,
                    download_cap_bytes=download_cap_bytes,
                ) as emoji_path:
                    if await helper_check_attachment(
                        self,
                        message.author,
                        emoji_path,
                        nsfw_callback,
                        guild_id,
                        message,
                        settings_cache=settings_cache,
                    ):
                        return True
            except ValueError as download_error:
                log.debug(
                    "Skipping emoji %s due to download cap: %s",
                    emoji_url,
                    download_error,
                )
            except Exception as e:
                print(f"[emoji-scan] Failed to scan custom emoji {emoji_obj}: {e}")

        return False
