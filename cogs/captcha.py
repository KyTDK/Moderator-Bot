from discord.ext import commands
from cogs.captcha.cog import setup_captcha

async def setup(bot: commands.Bot):
    await setup_captcha(bot)