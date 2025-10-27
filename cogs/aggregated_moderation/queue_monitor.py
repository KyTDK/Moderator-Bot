from __future__ import annotations

import asyncio
import math
import time
from typing import Optional

import discord

from modules.nsfw_scanner.constants import LOG_CHANNEL_ID
from modules.utils.discord_utils import safe_get_channel

from .config import AggregatedModerationConfig
from .queue_snapshot import QueueSnapshot


class FreeQueueMonitor:
    """Observes queue health, surfaces lag, and optionally adapts limits."""

    ADAPTIVE_COOLDOWN = 15.0
    ADAPTIVE_MULTIPLIER_CAP = 8.0
    ADAPTIVE_EXTRA_STEPS = 16

    def __init__(
        self,
        *,
        bot,
        free_queue,
        accelerated_queue,
        config: AggregatedModerationConfig,
    ) -> None:
        self._bot = bot
        self._free_queue = free_queue
        self._accelerated_queue = accelerated_queue
        self._config = config

        self._task: Optional[asyncio.Task] = None
        self._lag_hits: int = 0
        self._last_report_at: Optional[float] = None
        self._last_dropped_total: int = 0
        self._adaptive_last: dict[str, float] = {}

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        loop = asyncio.get_running_loop()
        self._task = loop.create_task(self._run())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None
            self._lag_hits = 0
            self._last_report_at = None
            self._last_dropped_total = 0
            self._adaptive_last.clear()

    @staticmethod
    def _is_lagging(free: QueueSnapshot, accel: QueueSnapshot) -> bool:
        backlog_pressure = free.backlog_high is not None and free.backlog >= free.backlog_high
        saturation = free.backlog > 0 and free.active_workers >= free.max_workers
        high_ratio = free.backlog_high is not None and free.backlog >= int(free.backlog_high * 1.5)
        relative_gap = (free.backlog - accel.backlog) >= max(5, accel.max_workers)
        backlog_growth = free.backlog >= free.max_workers * 4
        near_hard_limit = free.backlog_hard_limit is not None and free.backlog >= max(free.backlog_hard_limit - 5, 0)
        burst_saturated = free.backlog > free.autoscale_max and free.active_workers >= free.max_workers

        return backlog_pressure and (
            saturation
            or high_ratio
            or relative_gap
            or backlog_growth
            or near_hard_limit
            or burst_saturated
        )

    async def _emit_report(self, free: QueueSnapshot, accel: QueueSnapshot) -> None:
        ratio = free.backlog_ratio

        dropped_delta = max(0, free.dropped_total - self._last_dropped_total)
        self._last_dropped_total = free.dropped_total

        summary = [
            f"free_backlog={free.backlog}",
            f"free_workers={free.active_workers}/{free.max_workers}",
            f"accelerated_backlog={accel.backlog}",
            f"dropped_total={free.dropped_total}",
            f"avg_run={free.avg_runtime:.2f}s",
            f"avg_wait={free.avg_wait:.2f}s",
        ]
        if free.backlog_high:
            summary.append(f"backlog_ratio={ratio:.2f}")
        if dropped_delta:
            summary.append(f"dropped_since_last={dropped_delta}")

        print(f"[FreeQueueLag] {' '.join(summary)}")

        description = (
            f"Free backlog {free.backlog} (~{ratio:.2f}x high watermark)"
            if free.backlog_high
            else f"Free backlog {free.backlog}"
        )

        embed = discord.Embed(
            title="Free queue backlog warning",
            description=description,
            color=discord.Color.orange(),
        )
        embed.add_field(name="Free queue", value=free.format_lines(), inline=False)
        embed.add_field(name="Accelerated queue", value=accel.format_lines(), inline=False)
        embed.add_field(
            name="Current tuning snapshot",
            value="\n".join(
                [
                    f"FREE workers: base {free.baseline_workers} / current {free.max_workers} / burst {free.autoscale_max} (adaptive={'on' if self._config.free.adaptive_limits else 'off'})",
                    f"ACCEL workers: base {accel.baseline_workers} / current {accel.max_workers} / burst {accel.autoscale_max} (adaptive={'on' if self._config.accelerated.adaptive_limits else 'off'})",
                    f"Watermarks: high={free.backlog_high} / {accel.backlog_high}, low={free.backlog_low} / {accel.backlog_low}",
                ]
            ),
            inline=False,
        )
        embed.add_field(
            name="Longest free task breakdown",
            value=free.format_longest_runtime_detail(),
            inline=False,
        )
        embed.add_field(
            name="Latest free task snapshot",
            value=free.format_last_runtime_detail(),
            inline=False,
        )
        embed.set_footer(text=f"Dropped tasks since last report: {dropped_delta}")

        if not LOG_CHANNEL_ID:
            return
        try:
            channel = await safe_get_channel(self._bot, LOG_CHANNEL_ID)
            if channel is None:
                print(f"[FreeQueueLag] Unable to resolve LOG_CHANNEL_ID={LOG_CHANNEL_ID}")
                return
            await channel.send(embed=embed)
        except Exception as exc:  # noqa: BLE001
            print(f"[FreeQueueLag] Failed to send log embed: {exc}")

    def _adaptive_recommend(self, snapshot: QueueSnapshot) -> Optional[int]:
        if snapshot.backlog <= 0:
            return None

        base_capacity = snapshot.capacity
        backlog_high = snapshot.backlog_high or 0

        if backlog_high <= 0 and snapshot.backlog <= base_capacity:
            return None

        wait_pressure = snapshot.wait_pressure
        backlog_excess = snapshot.backlog_excess

        if backlog_excess <= 0 and not wait_pressure:
            return None

        increments = 0
        if backlog_high > 0 and backlog_excess > 0:
            increments = max(1, math.ceil(backlog_excess / backlog_high))
        elif backlog_excess > 0:
            increments = max(1, math.ceil(backlog_excess / snapshot.baseline_workers))
        elif wait_pressure:
            increments = 1

        if increments <= 0:
            return None

        target = base_capacity + increments * snapshot.baseline_workers

        if backlog_high > 0 and snapshot.backlog >= backlog_high:
            ratio = snapshot.backlog_ratio
            target = max(target, snapshot.baseline_workers * max(2, math.ceil(ratio)))

        if wait_pressure:
            target = max(target, base_capacity + snapshot.baseline_workers)

        cap_multiplier = self.ADAPTIVE_MULTIPLIER_CAP
        max_allowed = max(
            int(base_capacity * cap_multiplier),
            base_capacity + snapshot.baseline_workers * self.ADAPTIVE_EXTRA_STEPS,
        )
        target = min(target, max_allowed)

        backlog_low = snapshot.backlog_low or 0
        if backlog_low > 0:
            target = max(target, snapshot.baseline_workers + 1)

        target = min(target, snapshot.backlog)
        if target <= base_capacity:
            return None

        return target

    async def _maybe_apply_adaptive_scale(
        self,
        *,
        queue,
        tier: str,
        snapshot: QueueSnapshot,
        target: int,
        now: float,
    ) -> None:
        base_capacity = snapshot.capacity
        if target <= base_capacity:
            return

        last_action = self._adaptive_last.get(tier)
        if last_action is not None and (now - last_action) < self.ADAPTIVE_COOLDOWN:
            return

        self._adaptive_last[tier] = now
        backlog_high = snapshot.backlog_high if snapshot.backlog_high is not None else 0
        print(
            f"[FreeQueueLag] Adaptive scaling for {tier}: workers {base_capacity} -> {target} "
            f"(backlog={snapshot.backlog}, high={backlog_high}, avg_wait={snapshot.avg_wait:.2f}s)"
        )
        await queue.ensure_capacity(target)

    async def _handle_adaptive_scaling(self, free: QueueSnapshot, accel: QueueSnapshot) -> None:
        """Adjust queue limits when adaptive scaling is enabled."""
        now = time.monotonic()
        tasks = []

        if self._config.free.adaptive_limits:
            target = self._adaptive_recommend(free)
            if target is not None:
                tasks.append(
                    self._maybe_apply_adaptive_scale(
                        queue=self._free_queue,
                        tier="free",
                        snapshot=free,
                        target=target,
                        now=now,
                    )
                )

        if self._config.accelerated.adaptive_limits:
            target = self._adaptive_recommend(accel)
            if target is not None:
                tasks.append(
                    self._maybe_apply_adaptive_scale(
                        queue=self._accelerated_queue,
                        tier="accelerated",
                        snapshot=accel,
                        target=target,
                        now=now,
                    )
                )

        if tasks:
            await asyncio.gather(*tasks)

    async def _run(self) -> None:
        await self._bot.wait_until_ready()
        monitor_cfg = self._config.monitor
        try:
            while True:
                try:
                    await asyncio.sleep(monitor_cfg.check_interval)
                    if not self._free_queue.running:
                        continue

                    free_snapshot = QueueSnapshot.from_mapping(self._free_queue.metrics())
                    accel_snapshot = QueueSnapshot.from_mapping(self._accelerated_queue.metrics())

                    await self._handle_adaptive_scaling(free_snapshot, accel_snapshot)

                    if self._is_lagging(free_snapshot, accel_snapshot):
                        self._lag_hits += 1
                    else:
                        self._lag_hits = 0

                    if self._lag_hits < monitor_cfg.required_hits:
                        continue

                    now = time.monotonic()
                    if self._last_report_at is not None and (now - self._last_report_at) < monitor_cfg.cooldown:
                        continue

                    await self._emit_report(free_snapshot, accel_snapshot)
                    self._last_report_at = now
                    self._lag_hits = 0
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    print(f"[FreeQueueLag] Monitor iteration failed: {exc}")
                    self._lag_hits = 0
        except asyncio.CancelledError:
            raise


__all__ = ["FreeQueueMonitor"]
