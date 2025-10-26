
from discord.ext import commands
from cogs.aggregated_moderation.cog import setup as setup_cog

async def setup(bot: commands.Bot):
    await setup_cog(bot)
