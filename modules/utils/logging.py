from discord import Embed
from discord.ext import commands

async def log_to_channel(embed: Embed, channel_id: int, bot: commands.Bot, file=None):
    channel = await bot.fetch_channel(channel_id)
    if file:
        await channel.send(embed=embed, files=[file])
    else:
        await channel.send(embed=embed)