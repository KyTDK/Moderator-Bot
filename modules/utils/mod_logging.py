from discord import Embed, Forbidden, HTTPException
from discord.ext import commands

from modules.utils import mysql
from modules.utils.discord_utils import safe_get_channel

async def log_to_channel(embed: Embed, channel_id: int, bot: commands.Bot, file=None):
    try:
        channel = await safe_get_channel(bot, channel_id)

        is_accelerated = await mysql.is_accelerated(guild_id=channel.guild.id)
        if embed: 
            if not is_accelerated:
                embed.set_footer(
                    text=(
                        "Upgrade to Accelerated for faster NSFW & scam detection → /accelerated"
                    )
                )

        if channel is None:
            print(f"[log_to_channel] Channel {channel_id} not found.")
            return
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