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
}

def cache_message(msg: discord.Message):
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
    })

    message_cache.set(key, value, expire=86400) # TTL 24 hours

def get_cached_message(guild_id: int, message_id: int) -> dict | None:
    return message_cache.get(f"{guild_id}:{message_id}")
