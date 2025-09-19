import asyncio
import os
import uuid
import re
from tempfile import NamedTemporaryFile
from urllib.parse import urlparse

import aiohttp
import discord
from apnggif import apnggif
from cogs.hydration import wait_for_hydration
from discord.errors import NotFound
from discord.ext import commands
import pillow_avif  # registers AVIF support

from modules.utils import mysql

from .constants import TMP_DIR
from .helpers import (
    check_attachment as helper_check_attachment,
    temp_download as helper_temp_download,
)
from .utils import safe_delete



class NSFWScanner:
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session = None
        self.tmp_dir = TMP_DIR

    async def start(self):
        self.session = aiohttp.ClientSession()
        os.makedirs(self.tmp_dir, exist_ok=True)

    async def stop(self):
        if self.session:
            await self.session.close()

    async def is_nsfw(
        self,
        message: discord.Message | None = None,
        guild_id: int | None = None,
        nsfw_callback=None,
        url: str | None = None,
        member: discord.Member | None = None,
    ) -> bool:

        if url:
            async with helper_temp_download(self.session, url) as temp_filename:
                return await helper_check_attachment(self, member, temp_filename, nsfw_callback, guild_id, message)
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
                if await helper_check_attachment(self, message.author, temp_filename, nsfw_callback, guild_id, message):
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
                # Don't scan if its tenor and check-tenor-gifs is False
                if is_tenor and not await mysql.get_settings(guild_id, "check-tenor-gifs"):
                    continue
                async with helper_temp_download(self.session, gif_url) as temp_filename:
                    if await helper_check_attachment(
                        self,
                        author=message.author,
                        temp_filename=temp_filename,
                        nsfw_callback=nsfw_callback,
                        guild_id=guild_id,
                        message=message,
                    ):
                        return True

        for sticker in stickers:
            sticker_url = sticker.url
            if not sticker_url:
                continue

            extension = sticker.format.name.lower()

            async with helper_temp_download(self.session, sticker_url, ext=extension) as temp_location:
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
                    ):
                        return True
                finally:
                    if gif_location != temp_location:
                        safe_delete(gif_location)

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
                async with helper_temp_download(self.session, emoji_url) as emoji_path:
                    if await helper_check_attachment(
                        self,
                        message.author,
                        emoji_path,
                        nsfw_callback,
                        guild_id,
                        message,
                    ):
                        return True
            except Exception as e:
                print(f"[emoji-scan] Failed to scan custom emoji {emoji_obj}: {e}")

        return False
    