from discord import Embed, Forbidden, HTTPException
from discord.ext import commands

from modules.utils import mysql
from modules.utils import discord_utils

async def log_to_channel(embed: Embed, channel_id: int, bot: commands.Bot, file=None):
    safe_get_channel = getattr(discord_utils, "safe_get_channel", None)
    if safe_get_channel is None:
        raise RuntimeError("safe_get_channel is unavailable in modules.utils.discord_utils")

    channel = None
    guild_id = None
    try:
        channel = await safe_get_channel(bot, channel_id)

        if channel is None:
            print(f"[log_to_channel] Channel {channel_id} not found.")
            return

        guild = getattr(channel, "guild", None)
        guild_id = getattr(guild, "id", None)
        if isinstance(guild_id, str):
            try:
                guild_id = int(guild_id)
            except ValueError:
                print(f"[log_to_channel] Invalid guild ID format for channel {channel_id}: {guild_id}")
                return

        if guild_id is None:
            print(f"[log_to_channel] Missing guild context for channel {channel_id}.")

        is_accelerated = False
        if guild_id is not None:
            is_accelerated = await mysql.is_accelerated(guild_id=guild_id)

        if embed and not is_accelerated:
            translator = getattr(bot, "translate", None)
            fallback = (
                "Upgrade to Accelerated for faster NSFW & scam detection → /accelerated"
            )
            footer_text = (
                translator(
                    "promo_footer",
                    guild_id=guild_id,
                    placeholders={"command": "/accelerated"},
                    fallback=fallback,
                )
                if callable(translator)
                else fallback
            )
            embed.set_footer(text=footer_text)

        if file:
            await channel.send(embed=embed, files=[file])
        else:
            await channel.send(embed=embed)

    except Forbidden:
        guild_text = f" (guild {guild_id})" if guild_id is not None else ""
        print(f"[log_to_channel] Missing permission to send messages in channel {channel_id}{guild_text}.")
    except HTTPException as e:
        guild_text = f", guild {guild_id}" if guild_id is not None else ""
        print(f"[log_to_channel] HTTP error while sending message to channel {channel_id}{guild_text}: {e}")
    except Exception as e:
        guild_text = f", guild {guild_id}" if guild_id is not None else ""
        print(f"[log_to_channel] Unexpected error for channel {channel_id}{guild_text}: {e}")
