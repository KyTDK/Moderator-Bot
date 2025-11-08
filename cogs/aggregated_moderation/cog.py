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

from .adaptive_controller import AdaptiveQueueController
from .config import AggregatedModerationConfig, load_config
from .handlers import ModerationHandlers
from .queue_monitor import FreeQueueMonitor


class AggregatedModerationCog(commands.Cog):
    def __init__(self, bot: ModeratorBot):
        self.bot = bot
        self.config: AggregatedModerationConfig = load_config()

        self.scanner = NSFWScanner(bot)
        self._singular_task_reporter = SingularTaskReporter(bot)

        free_policy = self.config.free_policy
        accel_policy = self.config.accelerated_policy
        controller_cfg = self.config.controller

        self.free_queue = WorkerQueue(
            max_workers=free_policy.min_workers,
            autoscale_max=free_policy.min_workers,
            backlog_high_watermark=free_policy.backlog_soft_limit,
            backlog_low_watermark=max(1, free_policy.backlog_low),
            autoscale_check_interval=controller_cfg.tick_interval,
            scale_down_grace=max(5.0, controller_cfg.scale_down_cooldown),
            name="free",
            singular_task_reporter=self._singular_task_reporter,
            developer_log_bot=bot,
            developer_log_context="aggregated_moderation.free_queue",
            adaptive_mode=True,
            rate_tracking_window=controller_cfg.rate_window,
        )
        self.accelerated_queue = WorkerQueue(
            max_workers=accel_policy.min_workers,
            autoscale_max=accel_policy.min_workers,
            backlog_high_watermark=accel_policy.backlog_soft_limit,
            backlog_low_watermark=max(0, accel_policy.backlog_low),
            autoscale_check_interval=controller_cfg.tick_interval,
            scale_down_grace=max(5.0, controller_cfg.scale_down_cooldown),
            name="accelerated",
            singular_task_reporter=self._singular_task_reporter,
            developer_log_bot=bot,
            developer_log_context="aggregated_moderation.accelerated_queue",
            adaptive_mode=True,
            rate_tracking_window=controller_cfg.rate_window,
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
        self._adaptive_controller = AdaptiveQueueController(
            free_queue=self.free_queue,
            accelerated_queue=self.accelerated_queue,
            free_policy=free_policy,
            accelerated_policy=accel_policy,
            config=controller_cfg,
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
        await self._adaptive_controller.start()
        await self.queue_monitor.start()

    async def cog_unload(self):
        await self._adaptive_controller.stop()
        await self.scanner.stop()
        await self.free_queue.stop()
        await self.accelerated_queue.stop()
        await self.queue_monitor.stop()

async def setup(bot: commands.Bot):
    await bot.add_cog(AggregatedModerationCog(bot))

__all__ = ["AggregatedModerationCog"]
