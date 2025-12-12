from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import discord
from discord.ext import commands

from modules.cache import DEFAULT_CACHED_MESSAGE, CachedMessage, cache_message, get_cached_message
from modules.utils import mysql
from modules.utils.log_channel import DeveloperLogField, log_to_developer_channel
from modules.worker_queue import WorkerQueue
from modules.worker_queue_alerts import SingularTaskReporter


class QueueProfile:
    __slots__ = (
        "label",
        "queue",
        "baseline",
        "ceiling",
        "min_backlog_high",
        "min_backlog_low",
        "hard_limit_floor",
        "shed_to_floor",
        "high_multiplier",
        "shed_multiplier",
        "hard_multiplier",
        "shed_ratio",
    )

    def __init__(
        self,
        *,
        label: str,
        queue: WorkerQueue,
        baseline: int,
        ceiling: int,
        min_backlog_high: int,
        min_backlog_low: int,
        hard_limit_floor: int,
        shed_to_floor: int,
        high_multiplier: float = 3.5,
        shed_multiplier: float = 2.5,
        hard_multiplier: float = 6.0,
        shed_ratio: float | None = None,
    ) -> None:
        self.label = label
        self.queue = queue
        self.baseline = baseline
        self.ceiling = ceiling
        self.min_backlog_high = min_backlog_high
        self.min_backlog_low = min_backlog_low
        self.hard_limit_floor = hard_limit_floor
        self.shed_to_floor = shed_to_floor
        self.high_multiplier = high_multiplier
        self.shed_multiplier = shed_multiplier
        self.hard_multiplier = hard_multiplier
        self.shed_ratio = shed_ratio


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
        self._degraded_threshold = 210
        self._best_effort_active = False
        self._best_effort_overload_threshold = 140
        self._best_effort_recovery_threshold = 60
        self._best_effort_drop_cooldown = 15.0
        self._best_effort_skip_until = 0.0
        self._best_effort_suppressed = False
        self._surge_tiers: list[tuple[int, int]] = [
            (90, 10),
            (150, 16),
            (210, 22),
            (270, 28),
            (340, 36),
            (420, 42),
            (520, 48),
        ]
        self._surge_cooldown = 8.0
        self._last_surge = 0.0
        self._accelerated_surge_tiers: list[tuple[int, int]] = [
            (60, 14),
            (100, 20),
            (140, 26),
            (200, 32),
            (260, 40),
        ]
        self._accelerated_surge_cooldown = 8.0
        self._last_accelerated_surge = 0.0

        self.free_queue = WorkerQueue(
            max_workers=6,
            autoscale_max=48,
            backlog_high_watermark=200,
            backlog_low_watermark=60,
            backlog_hard_limit=1500,
            backlog_shed_to=1000,
            name="event_dispatcher_free",
            singular_task_reporter=self._singular_task_reporter,
            developer_log_bot=bot,
            developer_log_context="event_dispatcher.free_queue",
            adaptive_mode=True,
        )
        self.best_effort_queue = WorkerQueue(
            max_workers=6,
            autoscale_max=18,
            backlog_high_watermark=90,
            backlog_low_watermark=20,
            backlog_hard_limit=300,
            backlog_shed_to=160,
            name="event_dispatcher_best_effort",
            singular_task_reporter=self._singular_task_reporter,
            developer_log_bot=bot,
            developer_log_context="event_dispatcher.best_effort_queue",
            adaptive_mode=True,
        )
        self.accelerated_queue = WorkerQueue(
            max_workers=12,
            autoscale_max=40,
            backlog_high_watermark=85,
            backlog_low_watermark=18,
            backlog_hard_limit=520,
            backlog_shed_to=210,
            name="event_dispatcher_accelerated",
            singular_task_reporter=self._singular_task_reporter,
            developer_log_bot=bot,
            developer_log_context="event_dispatcher.accelerated_queue",
            adaptive_mode=True,
        )

        self._queue_profiles: dict[str, QueueProfile] = {
            "free": QueueProfile(
                label="free",
                queue=self.free_queue,
                baseline=6,
                ceiling=72,
                min_backlog_high=200,
                min_backlog_low=60,
                hard_limit_floor=1400,
                shed_to_floor=650,
                high_multiplier=4.0,
                shed_multiplier=2.5,
                hard_multiplier=8.0,
                shed_ratio=0.72,
            ),
            "best_effort": QueueProfile(
                label="best_effort",
                queue=self.best_effort_queue,
                baseline=4,
                ceiling=24,
                min_backlog_high=90,
                min_backlog_low=20,
                hard_limit_floor=300,
                shed_to_floor=140,
                high_multiplier=2.8,
                shed_multiplier=2.2,
                hard_multiplier=5.0,
            ),
            "accelerated": QueueProfile(
                label="accelerated",
                queue=self.accelerated_queue,
                baseline=12,
                ceiling=60,
                min_backlog_high=85,
                min_backlog_low=18,
                hard_limit_floor=520,
                shed_to_floor=210,
                high_multiplier=3.2,
                shed_multiplier=2.0,
                hard_multiplier=6.0,
            ),
        }
        self._last_capacity_plans: dict[str, dict[str, int]] = {}
        self._capacity_sample_interval = 2.5
        self._capacity_task: asyncio.Task | None = None
        self._resource_alert_threshold = 0.9
        self._resource_alert_cooldown = 120.0
        self._resource_alert_last: dict[str, float] = {}

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

        best_effort_target = self._resolve_best_effort_target(degraded, backlog)
        if best_effort_target is None:
            if degraded or self._best_effort_suppressed:
                return
            target_queue = queue
        else:
            target_queue = best_effort_target
        for name in self._BEST_EFFORT_COGS:
            await self._enqueue_cog(target_queue, name, message, batch_label="best_effort", best_effort=True)

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

    def _resolve_best_effort_target(self, degraded: bool, primary_backlog: int) -> WorkerQueue | None:
        if not self.best_effort_queue.running:
            return None
        backlog = self._queue_backlog(self.best_effort_queue)
        now = time.monotonic()

        if degraded:
            if not self._best_effort_suppressed:
                self._best_effort_suppressed = True
                self._best_effort_skip_until = now + self._best_effort_drop_cooldown
            return None

        if backlog >= self._best_effort_overload_threshold:
            self._best_effort_skip_until = now + self._best_effort_drop_cooldown
            if not self._best_effort_suppressed:
                self._log.warning(
                    "Best-effort queue overloaded (backlog=%s while primary backlog=%s); temporarily dropping optional cogs",
                    backlog,
                    primary_backlog,
                )
            self._best_effort_suppressed = True
            return None

        if self._best_effort_suppressed:
            if now >= self._best_effort_skip_until and backlog <= max(1, self._best_effort_recovery_threshold):
                self._best_effort_suppressed = False
                self._log.info("Best-effort queue recovered; resuming optional cogs")
            else:
                return None

        return self.best_effort_queue

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

    async def _capacity_manager(self) -> None:
        await self.bot.wait_until_ready()
        try:
            while True:
                await asyncio.sleep(self._capacity_sample_interval)
                try:
                    await self._rebalance_queue_capacity()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._log.exception("Dynamic capacity pass failed")
        except asyncio.CancelledError:
            pass

    async def _rebalance_queue_capacity(self) -> None:
        plan_keys = ("target", "baseline", "backlog_high", "backlog_low", "backlog_hard_limit", "backlog_shed_to")
        for profile in self._queue_profiles.values():
            queue = profile.queue
            if not queue.running:
                continue
            metrics = queue.metrics()
            plan = self._derive_queue_plan(profile, metrics)
            if not plan:
                continue
            last_plan = self._last_capacity_plans.get(profile.label)
            needs_update = last_plan is None or any(last_plan.get(key) != plan.get(key) for key in plan_keys)
            self._last_capacity_plans[profile.label] = plan
            if needs_update:
                try:
                    await queue.update_adaptive_plan(
                        target_workers=plan["target"],
                        baseline_workers=plan["baseline"],
                        backlog_high=plan["backlog_high"],
                        backlog_low=plan["backlog_low"],
                        backlog_hard_limit=plan["backlog_hard_limit"],
                        backlog_shed_to=plan["backlog_shed_to"],
                    )
                except Exception:
                    self._log.exception("Failed to apply adaptive plan for %s queue", profile.label)
            await self._emit_resource_alert(profile, metrics, plan)
        self._update_dynamic_thresholds()

    def _derive_queue_plan(self, profile: QueueProfile, metrics: dict[str, Any]) -> dict[str, int]:
        backlog = int(metrics.get("backlog") or 0)
        backlog_high_floor = profile.min_backlog_high
        backlog_low_floor = profile.min_backlog_low
        arrival = float(metrics.get("arrival_rate_per_min") or 0.0)
        completion = float(metrics.get("completion_rate_per_min") or 0.0)
        wait_signal = max(
            float(metrics.get("ema_wait_time") or 0.0),
            float(metrics.get("avg_wait_time") or 0.0),
            float(metrics.get("last_wait_time") or 0.0),
        )
        runtime_signal = max(
            float(metrics.get("ema_runtime") or 0.0),
            float(metrics.get("avg_runtime") or 0.0),
            float(metrics.get("last_runtime") or 0.0),
        )
        if completion <= 0:
            ratio = 5.0 if arrival > 0 else 0.0
        else:
            ratio = max(0.0, arrival / max(completion, 0.001))
        backlog_pressure = backlog / max(backlog_high_floor, 1)

        target = profile.baseline
        backlog_boost = 0
        if backlog_pressure >= 1.5:
            backlog_boost = backlog // max(2, backlog_high_floor // 3 or 1)
        elif backlog_pressure >= 1.0:
            backlog_boost = backlog // max(3, backlog_high_floor // 3 or 1)
        elif backlog_pressure >= 0.6:
            backlog_boost = backlog // max(5, backlog_high_floor // 2 or 1)
        elif backlog_pressure >= 0.3:
            backlog_boost = max(1, backlog_high_floor // 12)
        target += backlog_boost

        if ratio > 1.05:
            ratio_boost = max(1, int(profile.baseline * min(ratio - 1.0, 4.0)))
            target += ratio_boost

        target = min(profile.ceiling, max(profile.baseline, target))
        baseline = max(
            profile.baseline,
            min(target - 1, int(max(profile.baseline, target * 0.6))),
        )
        if baseline > target:
            baseline = target

        backlog_high = max(
            backlog_high_floor,
            int(max(target * profile.high_multiplier, backlog_high_floor)),
        )
        backlog_low = max(
            backlog_low_floor,
            max(5, int(backlog_high * 0.35)),
        )
        backlog_hard_limit = max(
            profile.hard_limit_floor,
            int(max(backlog_high * profile.hard_multiplier, backlog_high + 1)),
        )
        shed_candidates = [
            backlog_high * profile.shed_multiplier,
            backlog_high,
        ]
        if profile.shed_ratio:
            shed_candidates.append(backlog_hard_limit * profile.shed_ratio)
        backlog_shed_to = max(
            profile.shed_to_floor,
            int(max(shed_candidates)),
        )
        backlog_shed_to = min(backlog_shed_to, backlog_hard_limit - 1)

        wait_pressure = wait_signal >= max(12.0, runtime_signal * 3.5)
        severe_backlog = backlog >= max(backlog_high * 2, int(backlog_hard_limit * 0.8))
        if wait_pressure or severe_backlog:
            target = profile.ceiling
            baseline = max(profile.baseline, min(target - 1, int(target * 0.7)))
            backlog_high = max(
                backlog_high,
                int(max(target * profile.high_multiplier, backlog_high_floor)),
            )
            backlog_low = max(
                backlog_low,
                max(backlog_low_floor, max(5, int(backlog_high * 0.35))),
            )
            backlog_hard_limit = max(
                backlog_hard_limit,
                int(max(backlog_high * profile.hard_multiplier, backlog_high + 1)),
            )
            shed_candidates = [
                backlog_shed_to,
                backlog_high * profile.shed_multiplier,
                backlog_high,
            ]
            if profile.shed_ratio:
                shed_candidates.append(backlog_hard_limit * profile.shed_ratio)
            backlog_shed_to = max(
                profile.shed_to_floor,
                int(max(shed_candidates)),
            )
            backlog_shed_to = min(backlog_shed_to, backlog_hard_limit - 1)

        return {
            "target": int(target),
            "baseline": int(baseline),
            "backlog_high": int(backlog_high),
            "backlog_low": int(backlog_low),
            "backlog_hard_limit": int(backlog_hard_limit),
            "backlog_shed_to": int(backlog_shed_to),
        }

    async def _emit_resource_alert(self, profile: QueueProfile, metrics: dict[str, Any], plan: dict[str, int]) -> None:
        hard_limit = plan.get("backlog_hard_limit") or 0
        if hard_limit <= 0:
            return
        backlog = int(metrics.get("backlog") or 0)
        ratio = backlog / hard_limit if hard_limit else 0.0
        severity = "warning"
        summary = f"{profile.label.title()} queue resource limit risk"
        if backlog >= hard_limit:
            severity = "critical"
            summary = f"{profile.label.title()} queue resource limit reached"
        elif ratio < self._resource_alert_threshold:
            return

        now = time.monotonic()
        last = self._resource_alert_last.get(profile.label, 0.0)
        if severity != "critical" and (now - last) < self._resource_alert_cooldown:
            return
        self._resource_alert_last[profile.label] = now

        arrival = float(metrics.get("arrival_rate_per_min") or 0.0)
        completion = float(metrics.get("completion_rate_per_min") or 0.0)
        busy = int(metrics.get("busy_workers") or 0)
        max_workers = int(metrics.get("max_workers") or plan.get("target") or 0)

        fields = [
            DeveloperLogField(name="Backlog", value=str(backlog), inline=True),
            DeveloperLogField(name="Hard Limit", value=str(hard_limit), inline=True),
            DeveloperLogField(name="Target Workers", value=str(plan.get("target")), inline=True),
            DeveloperLogField(name="Busy Workers", value=f"{busy}/{max_workers}", inline=True),
            DeveloperLogField(name="Arrival/min", value=f"{arrival:.2f}", inline=True),
            DeveloperLogField(name="Completion/min", value=f"{completion:.2f}", inline=True),
            DeveloperLogField(name="Backlog High", value=str(plan.get("backlog_high")), inline=True),
        ]

        description = (
            f"{profile.label} queue backlog is {backlog}/{hard_limit} "
            f"({ratio:.0%} of hard limit)."
        )
        success = await log_to_developer_channel(
            self.bot,
            summary=summary,
            severity=severity,
            description=description,
            fields=fields,
            context=f"event_dispatcher.{profile.label}",
        )
        if not success:
            self._log.debug("Failed to dispatch resource alert for %s queue", profile.label)

    def _update_dynamic_thresholds(self) -> None:
        free_plan = self._last_capacity_plans.get("free")
        if free_plan:
            base = int(free_plan.get("backlog_high") or self._degraded_threshold or 0)
            self._degraded_threshold = max(base, 45)
        best_effort_plan = self._last_capacity_plans.get("best_effort")
        if best_effort_plan:
            overload = int(best_effort_plan.get("backlog_high") or self._best_effort_overload_threshold or 0)
            overload = max(overload, 10)
            recovery_candidate = int(best_effort_plan.get("backlog_low") or self._best_effort_recovery_threshold or 0)
            recovery = max(5, min(overload - 5, max(recovery_candidate, 5)))
            self._best_effort_overload_threshold = overload
            self._best_effort_recovery_threshold = recovery

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
        if not after or not after.author or cached_before.content == after.content or after.author.bot:
            return

        # Populate missing author details when we fall back to defaults
        if getattr(cached_before, "author_id", None) is None:
            cached_before.author_id = getattr(after.author, "id", None)
            cached_before.author_name = str(after.author)
            cached_before.author_avatar = (
                str(after.author.avatar.url)
                if getattr(after.author, "avatar", None) is not None
                else None
            )
            cached_before.author_mention = getattr(after.author, "mention", None)

        # Handle message edit
        cogs_to_notify = (
            "AggregatedModerationCog",
            "BannedWordsCog",
            "MonitoringCog",
            "AutonomousModeratorCog",
        )
        for cog_name in cogs_to_notify:
            cog = self.bot.get_cog(cog_name)
            if not cog:
                self._log.warning("Skipping handle_message_edit; cog %s is not available", cog_name)
                continue
            await cog.handle_message_edit(cached_before, after)

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
        await self._rebalance_queue_capacity()
        if self._capacity_task is None or self._capacity_task.done():
            loop = asyncio.get_running_loop()
            self._capacity_task = loop.create_task(self._capacity_manager())

    async def cog_unload(self):
        if self._capacity_task is not None:
            self._capacity_task.cancel()
            try:
                await self._capacity_task
            except asyncio.CancelledError:
                pass
            finally:
                self._capacity_task = None
        await self.free_queue.stop()
        await self.best_effort_queue.stop()
        await self.accelerated_queue.stop()

async def setup(bot: commands.Bot):
    await bot.add_cog(EventDispatcherCog(bot))
