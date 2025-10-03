from discord import Embed, Forbidden, HTTPException
from discord.ext import commands

from modules.utils import mysql
from modules.utils.discord_utils import safe_get_channel

async def log_to_channel(embed: Embed, channel_id: int, bot: commands.Bot, file=None):
    try:
        channel = await safe_get_channel(bot, channel_id)

        if channel is None:
            print(f"[log_to_channel] Channel {channel_id} not found.")
            return

        # Ensure guild ID is an int
        guild_id = channel.guild.id
        if isinstance(guild_id, str):
            try:
                guild_id = int(guild_id)
            except ValueError:
                print(f"[log_to_channel] Invalid guild ID format: {guild_id}")
                return

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
        print(f"[log_to_channel] Missing permission to send messages in channel {channel_id}.")
    except HTTPException as e:
        print(f"[log_to_channel] HTTP error while sending message to channel {channel_id}: {e}")
    except Exception as e:
        print(f"[log_to_channel] Unexpected error: {e}")
