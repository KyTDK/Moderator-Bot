import asyncio
from collections import OrderedDict
import os
import tempfile

import aiohttp
import discord
import diskcache as dc

cache_dir = os.path.join(tempfile.gettempdir(), "modbot_messages")
message_cache = dc.Cache(cache_dir, size_limit=10**9)
MEMORY_FALLBACK_LIMIT = 512
memory_fallback: "OrderedDict[str, dict]" = OrderedDict()

DEFAULT_CACHED_MESSAGE = {
    "content": None,
    "author_id": None,
    "author_name": None,
    "author_avatar": None,
    "author_mention": None,
    "channel_id": None,
    "timestamp": None,
    "attachments": [],
    "embeds": [],
    "stickers": [],
    "guild_id": None,
    "message_id": None,
    "reactions": [],
}

class CachedAttachment:
    def __init__(self, data: dict):
        self.filename = data.get("filename")
        self.url = data.get("url")
        self.proxy_url = data.get("proxy_url")
        self.id = data.get("id")
        self.size = data.get("size")
        self.content_type = data.get("content_type")

    async def save(self, fp, *, seek_begin: bool = True) -> None:
        target_url = self.proxy_url or self.url
        if not target_url:
            raise RuntimeError("CachedAttachment is missing a usable URL")

        should_close = False
        handle = fp
        if isinstance(fp, (str, os.PathLike)):
            handle = open(os.fspath(fp), "wb")
            should_close = True

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(target_url) as resp:
                    resp.raise_for_status()
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        if not chunk:
                            continue
                        handle.write(chunk)
            if seek_begin and hasattr(handle, "seek"):
                handle.seek(0)
        finally:
            if should_close:
                handle.close()

class CachedReaction:
    def __init__(self, data: dict):
        self.emoji = data["emoji"]
        self.count = data["count"]

class CachedSticker:
    def __init__(self, data: dict):
        self.id = data["id"]
        self.name = data["name"]
        self.url = data["url"]
        self.format_type = data["format_type"]

class CachedMessage:
    def __init__(self, data: dict):
        self.content = data["content"]
        self.author_id = data["author_id"]
        self.author_name = data["author_name"]
        self.author_avatar = data["author_avatar"]
        self.author_mention = data["author_mention"]
        self.channel_id = data["channel_id"]
        self.timestamp = data["timestamp"]
        self.guild_id = data["guild_id"]
        self.message_id = data["message_id"]

        # Convert dicts back to objects
        self.attachments = [CachedAttachment(a) for a in data["attachments"]]
        self.embeds = [discord.Embed.from_dict(e) for e in data["embeds"]]
        self.stickers = [CachedSticker(s) for s in data["stickers"]]
        self.reactions = [CachedReaction(r) for r in data["reactions"]]

async def cache_message(msg: discord.Message):
    key = f"{msg.guild.id}:{msg.id}"
    value = DEFAULT_CACHED_MESSAGE.copy()

    value.update({
        "content": msg.content,
        "author_id": msg.author.id,
        "author_name": str(msg.author),
        "author_avatar": str(msg.author.avatar.url) if msg.author.avatar else None,
        "author_mention": msg.author.mention,
        "channel_id": msg.channel.id,
        "timestamp": msg.created_at.timestamp(),
        "attachments": [
            {
                "id": getattr(a, "id", None),
                "filename": a.filename,
                "url": a.url,
                "proxy_url": getattr(a, "proxy_url", None),
                "size": a.size,
                "content_type": getattr(a, "content_type", None),
            }
            for a in msg.attachments
        ] if msg.attachments else [],
        "embeds": [embed.to_dict() for embed in msg.embeds],
        "stickers": [
            {
                "id": s.id,
                "name": s.name,
                "url": s.url,
                "format_type": s.format.name,
            }
            for s in msg.stickers
        ] if msg.stickers else [],
        "guild_id": msg.guild.id if msg.guild else None,
        "message_id": msg.id,
        "reactions": [
            {
                "emoji": str(r.emoji),
                "count": r.count
            }
            for r in msg.reactions
        ] if msg.reactions else [],
    })

    # Keep a small in-memory fallback so reaction handlers can still work if diskcache bails out.
    memory_fallback[key] = value
    memory_fallback.move_to_end(key)
    if len(memory_fallback) > MEMORY_FALLBACK_LIMIT:
        memory_fallback.popitem(last=False)

    try:
        await asyncio.to_thread(message_cache.set, key, value, expire=86400)
    except Exception as exc:
        print(f"[cache] failed to persist message {msg.id} for guild {msg.guild.id if msg.guild else 'unknown'}: {exc}")

async def get_cached_message(guild_id: int, message_id: int) -> CachedMessage | None:
    key = f"{guild_id}:{message_id}"

    # Prefer the in-memory fallback first as it's already available without I/O
    data = memory_fallback.get(key)
    if data is None:
        try:
            data = await asyncio.to_thread(message_cache.get, key)
        except Exception as exc:
            print(
                f"[cache] failed to read message {message_id} for guild {guild_id}: {exc}"
            )
            data = None

    return CachedMessage(data) if data else None
