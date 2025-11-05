from __future__ import annotations

from datetime import timedelta

import discord
from discord.ext import commands
from discord.utils import utcnow

from modules.core.moderator_bot import ModeratorBot
from modules.nsfw_scanner import NSFWScanner
from modules.utils import mysql
from modules.worker_queue import WorkerQueue
from modules.worker_queue_alerts import SingularTaskReporter

from .config import AggregatedModerationConfig, load_config
from .handlers import ModerationHandlers
from .queue_monitor import FreeQueueMonitor


class AggregatedModerationCog(commands.Cog):
    def __init__(self, bot: ModeratorBot):
        self.bot = bot
        self.config: AggregatedModerationConfig = load_config()

        self.scanner = NSFWScanner(bot)
        self._singular_task_reporter = SingularTaskReporter(bot)

        autoscale = self.config.autoscale
        self.free_queue = WorkerQueue(
            max_workers=self.config.free.max_workers,
            autoscale_max=self.config.free.autoscale_max,
            backlog_high_watermark=autoscale.free_backlog_high,
            backlog_low_watermark=autoscale.backlog_low,
            autoscale_check_interval=autoscale.check_interval,
            scale_down_grace=autoscale.scale_down_grace,
            name="free",
            singular_task_reporter=self._singular_task_reporter,
            developer_log_bot=bot,
            developer_log_context="aggregated_moderation.free_queue",
        )
        self.accelerated_queue = WorkerQueue(
            max_workers=self.config.accelerated.max_workers,
            autoscale_max=self.config.accelerated.autoscale_max,
            backlog_high_watermark=autoscale.accelerated_backlog_high,
            backlog_low_watermark=autoscale.backlog_low,
            autoscale_check_interval=autoscale.check_interval,
            scale_down_grace=autoscale.scale_down_grace,
            name="accelerated",
            singular_task_reporter=self._singular_task_reporter,
            developer_log_bot=bot,
            developer_log_context="aggregated_moderation.accelerated_queue",
        )

        self.queue_monitor = FreeQueueMonitor(
            bot=bot,
            free_queue=self.free_queue,
            accelerated_queue=self.accelerated_queue,
            config=self.config,
        )
        self.handlers = ModerationHandlers(
            bot=bot,
            scanner=self.scanner,
            enqueue_task=self.add_to_queue,
        )

    def _is_new_guild(self, guild_id: int) -> bool:
        """Return True if the bot joined this guild within the last 30 minutes."""
        try:
            guild = self.bot.get_guild(guild_id)
            if not guild or not self.bot.user:
                return False
            me = guild.me or guild.get_member(self.bot.user.id)
            if not me or not me.joined_at:
                return False
            return (utcnow() - me.joined_at) <= timedelta(minutes=30)
        except Exception:
            return False

    async def add_to_queue(self, coro, guild_id: int):
        accelerated = await mysql.is_accelerated(guild_id=guild_id)
        if not accelerated and self._is_new_guild(guild_id):
            accelerated = True

        queue = self.accelerated_queue if accelerated else self.free_queue
        await queue.add_task(coro)

    async def handle_message(self, message: discord.Message):
        await self.handlers.handle_message(message)

    async def handle_message_edit(self, cached_before, after: discord.Message):
        await self.handlers.handle_message_edit(cached_before, after)

    @commands.Cog.listener()
    async def on_reaction_add(self, reaction, user):
        await self.handlers.handle_reaction_add(reaction, user)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        await self.handlers.handle_raw_reaction_add(payload)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        await self.handlers.handle_member_join(member)

    @commands.Cog.listener()
    async def on_user_update(self, before: discord.User, after: discord.User):
        await self.handlers.handle_user_update(before, after)

    async def cog_load(self):
        await self.scanner.start()
        await self.free_queue.start()
        await self.accelerated_queue.start()
        await self.queue_monitor.start()

    async def cog_unload(self):
        await self.scanner.stop()
        await self.free_queue.stop()
        await self.accelerated_queue.stop()
        await self.queue_monitor.stop()

async def setup(bot: commands.Bot):
    await bot.add_cog(AggregatedModerationCog(bot))

__all__ = ["AggregatedModerationCog"]
