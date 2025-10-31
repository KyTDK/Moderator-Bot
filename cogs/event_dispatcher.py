import asyncio
import discord
from discord.ext import commands

from modules.cache import DEFAULT_CACHED_MESSAGE, CachedMessage, cache_message, get_cached_message
from modules.utils import mysql
from modules.worker_queue import WorkerQueue
from modules.worker_queue_alerts import SingularTaskReporter

class EventDispatcherCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._singular_task_reporter = SingularTaskReporter(bot)
        self.free_queue = WorkerQueue(
            max_workers=1,
            name="event_dispatcher_free",
            singular_task_reporter=self._singular_task_reporter,
        )
        self.accelerated_queue = WorkerQueue(
            max_workers=5,
            name="event_dispatcher_accelerated",
            singular_task_reporter=self._singular_task_reporter,
        )

    async def add_to_queue(self, coro, guild_id: int):
        accelerated = await mysql.is_accelerated(guild_id=guild_id)

        queue = self.accelerated_queue if accelerated else self.free_queue
        await queue.add_task(coro)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        await cache_message(message)
        guild_id = message.guild.id

        cog_names = [
            "AggregatedModerationCog",
            "BannedWordsCog",
            "ScamDetectionCog",
            "AutonomousModeratorCog",
            "NicheModerationCog",
            "AdaptiveModerationCog",
            "BannedURLsCog",
        ]

        for name in cog_names:
            cog = self.bot.get_cog(name)
            if cog and hasattr(cog, "handle_message"):
                await self.add_to_queue(cog.handle_message(message), guild_id)

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent):
        # Get cache
        cached_before = await get_cached_message(payload.guild_id, payload.message_id)
        if not cached_before:
            # Make CachedMessage with defaults
            cached_before = CachedMessage(DEFAULT_CACHED_MESSAGE.copy())
            cached_before.guild_id = payload.guild_id
            cached_before.message_id = payload.message_id
            cached_before.channel_id = payload.channel_id
        
        # Check if the message was edited or if it's from a bot
        after = payload.message
        if cached_before.content == after.content or after.author.bot:
            return

        # Handle message edit
        await self.bot.get_cog("BannedWordsCog").handle_message_edit(cached_before, after)
        await self.bot.get_cog("MonitoringCog").handle_message_edit(cached_before, after)
        await self.bot.get_cog("AutonomousModeratorCog").handle_message_edit(cached_before, after)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        channel = self.bot.get_channel(payload.channel_id)
        if not channel or not isinstance(channel, discord.TextChannel):
            return

        # Get cached message or fallback with defaults
        cached_message = await get_cached_message(payload.guild_id, payload.message_id)
        if not cached_message:
            cached_message = CachedMessage(DEFAULT_CACHED_MESSAGE.copy())
            cached_message.guild_id = payload.guild_id
            cached_message.message_id = payload.message_id
            cached_message.channel_id = payload.channel_id

        # Handle message deletion
        await self.bot.get_cog("MonitoringCog").handle_message_delete(cached_message)

    async def cog_load(self):
        await self.free_queue.start()
        await self.accelerated_queue.start()

    async def cog_unload(self):
        await self.free_queue.stop()
        await self.accelerated_queue.stop()

async def setup(bot: commands.Bot):
    await bot.add_cog(EventDispatcherCog(bot))
