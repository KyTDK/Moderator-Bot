import asyncio
import discord
import diskcache as dc
import os, tempfile

cache_dir = os.path.join(tempfile.gettempdir(), "modbot_messages")
message_cache = dc.Cache(cache_dir, size_limit=10**9)

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
        self.filename = data["filename"]
        self.url = data["url"]
        self.size = data["size"]

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
                "filename": a.filename,
                "url": a.url,
                "size": a.size,
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

    try:
        await asyncio.to_thread(message_cache.set, key, value, expire=86400)
    except Exception as exc:
        print(f"[cache] failed to persist message {msg.id} for guild {msg.guild.id if msg.guild else 'unknown'}: {exc}")

def get_cached_message(guild_id: int, message_id: int) -> CachedMessage | None:
    data = message_cache.get(f"{guild_id}:{message_id}")
    return CachedMessage(data) if data else None
