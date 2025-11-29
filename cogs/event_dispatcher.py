from __future__ import annotations

import logging
import time

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
        self._degraded_threshold = 460
        self._best_effort_active = False
        self._best_effort_overload_threshold = 350
        self._best_effort_drop_cooldown = 15.0
        self._best_effort_skip_until = 0.0
        self._best_effort_suppressed = False
        self._surge_tiers: list[tuple[int, int]] = [
            (500, 18),
            (700, 28),
            (900, 38),
            (1100, 48),
            (1300, 56),
            (1500, 64),
        ]
        self._surge_cooldown = 8.0
        self._last_surge = 0.0
        self._accelerated_surge_tiers: list[tuple[int, int]] = [
            (160, 12),
            (240, 18),
            (320, 24),
            (420, 30),
            (520, 36),
        ]
        self._accelerated_surge_cooldown = 8.0
        self._last_accelerated_surge = 0.0

        self.free_queue = WorkerQueue(
            max_workers=2,
            autoscale_max=36,
            backlog_high_watermark=480,
            backlog_low_watermark=90,
            backlog_hard_limit=1800,
            backlog_shed_to=520,
            name="event_dispatcher_free",
            singular_task_reporter=self._singular_task_reporter,
            developer_log_bot=bot,
            developer_log_context="event_dispatcher.free_queue",
            adaptive_mode=False,
        )
        self.best_effort_queue = WorkerQueue(
            max_workers=4,
            autoscale_max=10,
            backlog_high_watermark=200,
            backlog_low_watermark=30,
            backlog_hard_limit=500,
            backlog_shed_to=180,
            name="event_dispatcher_best_effort",
            singular_task_reporter=self._singular_task_reporter,
            developer_log_bot=bot,
            developer_log_context="event_dispatcher.best_effort_queue",
            adaptive_mode=True,
        )
        self.accelerated_queue = WorkerQueue(
            max_workers=8,
            autoscale_max=28,
            backlog_high_watermark=140,
            backlog_low_watermark=20,
            backlog_hard_limit=650,
            backlog_shed_to=200,
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
        backlog = self._queue_backlog(queue)
        degraded = bool(not accelerated and backlog >= self._degraded_threshold)
        self._update_best_effort_state(degraded, backlog)
        if queue is self.free_queue:
            await self._maybe_trigger_surge(
                queue=self.free_queue,
                backlog=backlog,
                tiers=self._surge_tiers,
                cooldown_attr="_last_surge",
                cooldown_seconds=self._surge_cooldown,
                label="Free queue",
            )
        else:
            await self._maybe_trigger_surge(
                queue=self.accelerated_queue,
                backlog=backlog,
                tiers=self._accelerated_surge_tiers,
                cooldown_attr="_last_accelerated_surge",
                cooldown_seconds=self._accelerated_surge_cooldown,
                label="Accelerated queue",
            )

        for name in self._ESSENTIAL_COGS:
            await self._enqueue_cog(queue, name, message, batch_label="primary")

        best_effort_target = self._resolve_best_effort_target(degraded)
        if best_effort_target is None and degraded:
            return

        target_queue = best_effort_target or queue
        for name in self._BEST_EFFORT_COGS:
            await self._enqueue_cog(target_queue, name, message, batch_label="best_effort", best_effort=bool(best_effort_target))

    async def _resolve_queue_for_guild(self, guild_id: int) -> tuple[WorkerQueue, bool]:
        accelerated = await mysql.is_accelerated(guild_id=guild_id)
        return (self.accelerated_queue if accelerated else self.free_queue, accelerated)

    def _queue_backlog(self, queue: WorkerQueue) -> int:
        try:
            return queue.queue.qsize()
        except Exception:
            return 0

    async def _maybe_trigger_surge(
        self,
        *,
        queue: WorkerQueue,
        backlog: int,
        tiers: list[tuple[int, int]],
        cooldown_attr: str,
        cooldown_seconds: float,
        label: str,
    ) -> None:
        target_workers = 0
        threshold_hit = None
        for threshold, workers in tiers:
            if backlog >= threshold:
                target_workers = workers
                threshold_hit = threshold
        if not target_workers:
            return

        now = time.monotonic()
        last_surge = getattr(self, cooldown_attr, 0.0)
        if (now - last_surge) < cooldown_seconds:
            return

        setattr(self, cooldown_attr, now)
        try:
            await queue.ensure_capacity(target_workers)
        except Exception:
            self._log.exception(
                "%s surge failed (backlog=%s, threshold=%s, target=%s)",
                label,
                backlog,
                threshold_hit,
                target_workers,
            )
            return

        self._log.warning(
            "%s surge triggered; backlog=%s exceeded tier %s -> workers=%s",
            label,
            backlog,
            threshold_hit,
            target_workers,
        )

    def _resolve_best_effort_target(self, degraded: bool) -> WorkerQueue | None:
        if not degraded:
            self._adjust_best_effort_suppression(recovered=True)
            return None

        backlog = self._queue_backlog(self.best_effort_queue)
        now = time.monotonic()
        if backlog >= self._best_effort_overload_threshold:
            self._best_effort_skip_until = now + self._best_effort_drop_cooldown
            if not self._best_effort_suppressed:
                self._log.warning(
                    "Best-effort queue overloaded (backlog=%s); temporarily dropping optional cogs",
                    backlog,
                )
            self._best_effort_suppressed = True
            return None

        if self._best_effort_suppressed and now >= self._best_effort_skip_until and backlog <= max(1, self._best_effort_overload_threshold // 2):
            self._best_effort_suppressed = False
            self._log.info("Best-effort queue recovered; resuming optional cogs")

        return None if self._best_effort_suppressed else self.best_effort_queue

    async def _enqueue_cog(
        self,
        queue: WorkerQueue,
        name: str,
        message: discord.Message,
        *,
        batch_label: str,
        best_effort: bool = False,
    ) -> None:
        cog = self.bot.get_cog(name)
        handler = getattr(cog, "handle_message", None) if cog else None
        if handler is None:
            return
        try:
            await queue.add_task(handler(message))
        except Exception:
            self._log.exception(
                "Event dispatcher failed to enqueue %s (%s)",
                name,
                batch_label,
                extra={
                    "guild_id": getattr(getattr(message, "guild", None), "id", None),
                    "channel_id": getattr(getattr(message, "channel", None), "id", None),
                    "message_id": getattr(message, "id", None),
                    "best_effort": best_effort,
                },
            )

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
            self._adjust_best_effort_suppression(recovered=True)

    def _adjust_best_effort_suppression(self, *, recovered: bool) -> None:
        if recovered and self._best_effort_suppressed:
            self._best_effort_suppressed = False
            self._best_effort_skip_until = 0.0
            self._log.info("Best-effort drop window cleared after recovery")

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
