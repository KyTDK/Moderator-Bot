import discord
from discord.ext import commands

from modules.cache import cache_message, get_cached_message

class EventDispatcherCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        
        # Cache messages
        cache_message(message)

        # Handle message 
        await self.bot.get_cog("AggregatedModerationCog").handle_message(message)
        await self.bot.get_cog("BannedWordsCog").handle_message(message)
        await self.bot.get_cog("ScamDetectionCog").handle_message(message)
        await self.bot.get_cog("AutonomousModeratorCog").handle_message(message)
        await self.bot.get_cog("NicheModerationCog").handle_message(message)
        await self.bot.get_cog("AdaptiveModerationCog").handle_message(message)

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent):
        cached_before = get_cached_message(payload.guild_id, payload.message_id)
        if not cached_before:
            return
        
        after = payload.message

        if cached_before["content"] == after.content or after.author.bot:
            return

        # Handle message edit
        await self.bot.get_cog("BannedWordsCog").handle_message_edit(cached_before, after)
        await self.bot.get_cog("MonitoringCog").handle_message_edit(cached_before, after)
        await self.bot.get_cog("AutonomousModeratorCog").handle_message_edit(cached_before, after)

async def setup(bot: commands.Bot):
    await bot.add_cog(EventDispatcherCog(bot))
