import discord
from discord.ext import commands

class EventDispatcherCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        await self.bot.get_cog("AggregatedModerationCog").handle_message(message)
        await self.bot.get_cog("BannedWordsCog").handle_message(message)
        await self.bot.get_cog("ScamDetectionCog").handle_message(message)
        await self.bot.get_cog("MonitoringCog").handle_message(message)

async def setup(bot: commands.Bot):
    await bot.add_cog(EventDispatcherCog(bot))
