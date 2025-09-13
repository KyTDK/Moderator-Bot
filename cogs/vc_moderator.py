from discord.ext import commands
from cogs.voice_moderation.voice_moderator import setup_voice_moderation

async def setup(bot: commands.Bot):
    await setup_voice_moderation(bot)

