from discord.ext import commands

from cogs.verification.cog import setup_verification


async def setup(bot: commands.Bot) -> None:
    await setup_verification(bot)
