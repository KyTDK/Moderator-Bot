from __future__ import annotations

import time
import asyncio
from datetime import timedelta

import discord
from discord.ext import commands
from discord.utils import utcnow

from modules.core.moderator_bot import ModeratorBot
from modules.nsfw_scanner import NSFWScanner
from modules.nsfw_scanner import constants as nsfw_constants
from modules.utils import mysql
from modules.utils import log_channel as log_channel_module
from modules.utils.log_channel import DeveloperLogField
from modules.worker_queue import WorkerQueue
from modules.worker_queue_alerts import SingularTaskReporter

from .adaptive_controller import AdaptiveQueueController
from .config import AggregatedModerationConfig, load_config
from .handlers import ModerationHandlers
from .performance_monitor import AcceleratedPerformanceMonitor
from .queue_monitor import FreeQueueMonitor
from .queue_context import reset_current_queue, set_current_queue
from .queue_snapshot import QueueSnapshot

build_developer_log_embed = getattr(log_channel_module, "build_developer_log_embed", lambda **kwargs: None)
send_developer_log_embed = getattr(log_channel_module, "send_developer_log_embed", lambda *args, **kwargs: False)

_LOG_CHANNEL_ID = getattr(nsfw_constants, "LOG_CHANNEL_ID", 0)
_VIDEO_WALL_CLOCK_LIMIT = getattr(nsfw_constants, "VIDEO_SCAN_WALL_CLOCK_LIMIT_SECONDS", 105)

_VIDEO_TASK_TIMEOUT_SECONDS = max(90.0, min(240.0, (_VIDEO_WALL_CLOCK_LIMIT or 0) + 30.0))


class AggregatedModerationCog(commands.Cog):
    def __init__(self, bot: ModeratorBot):
        self.bot = bot
        self.config: AggregatedModerationConfig = load_config()

        self.scanner = NSFWScanner(bot)
        self._singular_task_reporter = SingularTaskReporter(bot)

        free_policy = self.config.free_policy
        accel_policy = self.config.accelerated_policy
        accel_text_policy = self.config.accelerated_text_policy
        controller_cfg = self.config.controller
        video_policy = self.config.video_policy

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
        self.accelerated_text_queue = WorkerQueue(
            max_workers=accel_text_policy.min_workers,
            autoscale_max=accel_text_policy.min_workers,
            backlog_high_watermark=accel_text_policy.backlog_soft_limit,
            backlog_low_watermark=max(0, accel_text_policy.backlog_low),
            autoscale_check_interval=controller_cfg.tick_interval,
            scale_down_grace=max(5.0, controller_cfg.scale_down_cooldown),
            name="accelerated_text",
            singular_task_reporter=self._singular_task_reporter,
            developer_log_bot=bot,
            developer_log_context="aggregated_moderation.accelerated_text_queue",
            adaptive_mode=True,
            rate_tracking_window=controller_cfg.rate_window,
        )
        self.video_queue = WorkerQueue(
            max_workers=video_policy.min_workers,
            autoscale_max=video_policy.max_workers,
            backlog_high_watermark=video_policy.backlog_soft_limit,
            backlog_low_watermark=max(0, video_policy.backlog_low),
            autoscale_check_interval=controller_cfg.tick_interval,
            scale_down_grace=max(5.0, controller_cfg.scale_down_cooldown),
            name="accelerated_video",
            singular_task_reporter=self._singular_task_reporter,
            singular_runtime_threshold=_VIDEO_TASK_TIMEOUT_SECONDS,
            developer_log_bot=bot,
            developer_log_context="aggregated_moderation.accelerated_video_queue",
            adaptive_mode=True,
            rate_tracking_window=controller_cfg.rate_window,
        )

        self.queue_monitor = FreeQueueMonitor(
            bot=bot,
            free_queue=self.free_queue,
            accelerated_queue=self.accelerated_queue,
            video_queue=self.video_queue,
            config=self.config,
        )
        self.performance_monitor = AcceleratedPerformanceMonitor(
            bot=bot,
            free_queue=self.free_queue,
            accelerated_queue=self.accelerated_queue,
            accelerated_text_queue=self.accelerated_text_queue,
            video_queue=self.video_queue,
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
            accelerated_text_queue=self.accelerated_text_queue,
            video_queue=self.video_queue,
            free_policy=free_policy,
            accelerated_policy=accel_policy,
            accelerated_text_policy=accel_text_policy,
            video_policy=video_policy,
            config=controller_cfg,
        )

        self._last_free_failover: float = 0.0
        self._failover_cooldown: float = 30.0
        self._video_task_timeout = _VIDEO_TASK_TIMEOUT_SECONDS

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

    def _free_queue_overloaded(self) -> bool:
        """Return True when free queue is saturated enough to justify failover."""

        if not self.free_queue.running:
            return False

        if self._last_free_failover:
            elapsed = time.monotonic() - self._last_free_failover
            if elapsed < self._failover_cooldown:
                return True

        try:
            snapshot = QueueSnapshot.from_mapping(self.free_queue.metrics())
        except Exception:
            return False

        backlog_high = snapshot.backlog_high or max(snapshot.baseline_workers * 3, 12)
        backlog_pressure = snapshot.backlog >= max(int(backlog_high * 1.25), backlog_high + snapshot.max_workers)
        hard_limit_pressure = False
        if snapshot.backlog_hard_limit is not None:
            hard_limit_pressure = snapshot.backlog >= max(snapshot.backlog_hard_limit - max(5, snapshot.max_workers), 0)

        wait_signal = snapshot.wait_signal()
        runtime_signal = snapshot.runtime_signal() or 0.0
        wait_pressure = wait_signal >= max(10.0, runtime_signal * 3.0)

        overloaded = backlog_pressure or hard_limit_pressure or wait_pressure
        if overloaded:
            self._last_free_failover = time.monotonic()
        return overloaded

    async def add_to_queue(self, coro, guild_id: int, *, task_kind: str | None = None):
        accelerated = await mysql.is_accelerated(guild_id=guild_id)
        if not accelerated and self._is_new_guild(guild_id):
            accelerated = True

        if not accelerated and self._free_queue_overloaded():
            accelerated = True

        if task_kind == "video" and accelerated:
            queue = self.video_queue
        elif task_kind == "text" and accelerated:
            queue = self.accelerated_text_queue
        else:
            queue = self.accelerated_queue if accelerated else self.free_queue
        queue_label = getattr(queue, "_name", None)

        async def run_with_queue_context():
            token = set_current_queue(queue_label)
            try:
                awaitable = coro
                if task_kind == "video" and self._video_task_timeout:
                    try:
                        awaitable = asyncio.wait_for(coro, timeout=float(self._video_task_timeout))
                    except Exception:
                        # If constructing the timeout wrapper fails for any reason, fall back to the raw task.
                        awaitable = coro
                try:
                    return await awaitable
                except asyncio.TimeoutError:
                    if task_kind == "video" and self._video_task_timeout:
                        timeout_summary = (
                            "[AggregatedModeration] Video task timed out after "
                            f"{self._video_task_timeout:.1f}s (guild_id={guild_id}, queue={queue_label})"
                        )
                        print(timeout_summary)

                        if _LOG_CHANNEL_ID:
                            metrics_summary = None
                            try:
                                metrics = queue.metrics()
                                metrics_fields = {
                                    "backlog": metrics.get("backlog"),
                                    "busy_workers": metrics.get("busy_workers"),
                                    "active_workers": metrics.get("active_workers"),
                                    "max_workers": metrics.get("max_workers"),
                                    "autoscale_max": metrics.get("autoscale_max"),
                                    "backlog_high": metrics.get("backlog_high"),
                                    "backlog_low": metrics.get("backlog_low"),
                                    "backlog_hard_limit": metrics.get("backlog_hard_limit"),
                                    "arrival_rate_per_min": metrics.get("arrival_rate_per_min"),
                                    "completion_rate_per_min": metrics.get("completion_rate_per_min"),
                                }
                                metrics_summary = "\n".join(
                                    f"{key}={value}" for key, value in metrics_fields.items() if value is not None
                                )
                            except Exception:
                                metrics_summary = None

                            embed_fields = [
                                DeveloperLogField(name="Guild", value=str(guild_id)),
                                DeveloperLogField(name="Queue", value=str(queue_label)),
                                DeveloperLogField(
                                    name="Timeout",
                                    value=f"{self._video_task_timeout:.1f}s",
                                ),
                            ]

                            if metrics_summary:
                                embed_fields.append(
                                    DeveloperLogField(
                                        name="Queue metrics", value=metrics_summary, inline=False
                                    )
                                )

                            embed = build_developer_log_embed(
                                title="AggregatedModeration video task timed out",
                                description=timeout_summary,
                                severity="error",
                                fields=embed_fields,
                                timestamp=True,
                            )

                            if not await send_developer_log_embed(
                                self.bot,
                                embed=embed,
                                context="aggregated_moderation.video_timeout",
                            ):
                                print(
                                    f"[AggregatedModeration] Failed to send video timeout to LOG_CHANNEL_ID={_LOG_CHANNEL_ID}"
                                )
                        return None
                    raise
            finally:
                reset_current_queue(token)

        await queue.add_task(run_with_queue_context())

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
        await self.accelerated_text_queue.start()
        await self.video_queue.start()
        await self._adaptive_controller.start()
        await self.queue_monitor.start()
        await self.performance_monitor.start()

    async def cog_unload(self):
        await self._adaptive_controller.stop()
        await self.scanner.stop()
        await self.free_queue.stop()
        await self.accelerated_queue.stop()
        await self.accelerated_text_queue.stop()
        await self.video_queue.stop()
        await self.queue_monitor.stop()
        await self.performance_monitor.stop()

async def setup(bot: commands.Bot):
    await bot.add_cog(AggregatedModerationCog(bot))

__all__ = ["AggregatedModerationCog"]
