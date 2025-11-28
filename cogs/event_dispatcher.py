from __future__ import annotations

import logging

import discord
from discord.ext import commands

from modules.cache import DEFAULT_CACHED_MESSAGE, CachedMessage, cache_message, get_cached_message
from modules.utils import mysql
from modules.worker_queue import WorkerQueue
from modules.worker_queue_alerts import SingularTaskReporter


class EventDispatcherCog(commands.Cog):
    _ESSENTIAL_COGS = [
        "AggregatedModerationCog",
        "BannedWordsCog",
        "ScamDetectionCog",
        "AutonomousModeratorCog",
    ]
    _BEST_EFFORT_COGS = [
        "NicheModerationCog",
        "AdaptiveModerationCog",
        "BannedURLsCog",
        "FAQCog",
    ]

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._log = logging.getLogger(f"{__name__}.EventDispatcher")
        self._singular_task_reporter = SingularTaskReporter(bot)
        self._degraded_threshold = 320
        self._best_effort_active = False

        self.free_queue = WorkerQueue(
            max_workers=3,
            autoscale_max=8,
            backlog_high_watermark=180,
            backlog_low_watermark=40,
            backlog_hard_limit=900,
            backlog_shed_to=250,
            name="event_dispatcher_free",
            singular_task_reporter=self._singular_task_reporter,
            developer_log_bot=bot,
            developer_log_context="event_dispatcher.free_queue",
            adaptive_mode=False,
        )
        self.best_effort_queue = WorkerQueue(
            max_workers=2,
            autoscale_max=4,
            backlog_high_watermark=150,
            backlog_low_watermark=25,
            backlog_hard_limit=500,
            backlog_shed_to=150,
            name="event_dispatcher_best_effort",
            singular_task_reporter=self._singular_task_reporter,
            developer_log_bot=bot,
            developer_log_context="event_dispatcher.best_effort_queue",
        )
        self.accelerated_queue = WorkerQueue(
            max_workers=6,
            autoscale_max=12,
            backlog_high_watermark=120,
            backlog_low_watermark=20,
            backlog_hard_limit=600,
            backlog_shed_to=180,
            name="event_dispatcher_accelerated",
            singular_task_reporter=self._singular_task_reporter,
            developer_log_bot=bot,
            developer_log_context="event_dispatcher.accelerated_queue",
        )

    async def add_to_queue(self, coro, guild_id: int):
        queue, _ = await self._resolve_queue_for_guild(guild_id)
        await queue.add_task(coro)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        await cache_message(message)
        guild_id = message.guild.id

        queue, accelerated = await self._resolve_queue_for_guild(guild_id)
        primary_cogs, deferred_cogs, degraded, backlog = self._select_cog_batches(
            queue=queue,
            accelerated=accelerated,
        )
        self._update_best_effort_state(degraded, backlog)

        primary_task = self._build_cog_runner(message, primary_cogs, "primary")
        if primary_task is not None:
            await queue.add_task(primary_task)

        if deferred_cogs:
            deferred_task = self._build_cog_runner(message, deferred_cogs, "best_effort")
            if deferred_task is not None:
                await self.best_effort_queue.add_task(deferred_task)

    async def _resolve_queue_for_guild(self, guild_id: int) -> tuple[WorkerQueue, bool]:
        accelerated = await mysql.is_accelerated(guild_id=guild_id)
        return (self.accelerated_queue if accelerated else self.free_queue, accelerated)

    def _select_cog_batches(
        self,
        *,
        queue: WorkerQueue,
        accelerated: bool,
    ) -> tuple[list[str], list[str], bool, int]:
        try:
            backlog = queue.queue.qsize()
        except Exception:
            backlog = 0

        degraded = bool(not accelerated and backlog >= self._degraded_threshold)
        primary = list(self._ESSENTIAL_COGS)
        deferred = list(self._BEST_EFFORT_COGS)
        if not degraded:
            primary.extend(deferred)
            deferred = []
        return primary, deferred, degraded, backlog

    def _build_cog_runner(self, message: discord.Message, cog_names: list[str], batch_label: str):
        if not cog_names:
            return None

        async def _runner():
            for name in cog_names:
                cog = self.bot.get_cog(name)
                handler = getattr(cog, "handle_message", None) if cog else None
                if handler is None:
                    continue
                try:
                    await handler(message)
                except Exception:
                    self._log.exception(
                        "Event dispatcher failed to execute %s.%s",
                        name,
                        batch_label,
                        extra={
                            "guild_id": getattr(getattr(message, "guild", None), "id", None),
                            "channel_id": getattr(getattr(message, "channel", None), "id", None),
                            "message_id": getattr(message, "id", None),
                        },
                    )

        return _runner()

    def _update_best_effort_state(self, degraded: bool, backlog: int) -> None:
        if degraded and not self._best_effort_active:
            self._best_effort_active = True
            self._log.warning(
                "Event dispatcher entering degraded mode; backlog=%s (best-effort cogs deferred)",
                backlog,
            )
        elif not degraded and self._best_effort_active:
            self._best_effort_active = False
            self._log.info("Event dispatcher recovered; backlog back under threshold")

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
        await self.bot.get_cog("AggregatedModerationCog").handle_message_edit(cached_before, after)
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
        await self.best_effort_queue.start()
        await self.accelerated_queue.start()

    async def cog_unload(self):
        await self.free_queue.stop()
        await self.best_effort_queue.stop()
        await self.accelerated_queue.stop()

async def setup(bot: commands.Bot):
    await bot.add_cog(EventDispatcherCog(bot))
