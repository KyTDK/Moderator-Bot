from __future__ import annotations

import asyncio
import time
from typing import Optional

import discord

from modules.nsfw_scanner.constants import LOG_CHANNEL_ID
from modules.utils.discord_utils import safe_get_channel

from .config import AggregatedModerationConfig


class FreeQueueMonitor:
    """Observes the free queue and surfaces lag warnings with tuning data."""

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

    def _is_lagging(self, free_metrics: dict, accel_metrics: dict) -> bool:
        backlog = int(free_metrics.get("backlog", 0))
        active = int(free_metrics.get("active_workers", 0))
        max_workers = max(1, int(free_metrics.get("max_workers", 1)))
        autoscale_max = max(int(free_metrics.get("autoscale_max", max_workers)), max_workers)
        backlog_high = int(free_metrics.get("backlog_high") or 0)
        hard_limit = free_metrics.get("backlog_hard_limit")
        accel_backlog = int(accel_metrics.get("backlog", 0))

        backlog_pressure = backlog_high > 0 and backlog >= backlog_high
        saturation = backlog > 0 and active >= max_workers
        high_ratio = backlog_high > 0 and backlog >= int(backlog_high * 1.5)
        relative_gap = backlog - accel_backlog >= max(5, int(accel_metrics.get("max_workers", 1)))
        backlog_growth = backlog >= max_workers * 4
        near_hard_limit = hard_limit is not None and backlog >= max(int(hard_limit) - 5, 0)
        burst_saturated = backlog > autoscale_max and active >= max_workers

        return backlog_pressure and (
            saturation
            or high_ratio
            or relative_gap
            or backlog_growth
            or near_hard_limit
            or burst_saturated
        )

    @staticmethod
    def _format_metrics(metrics: dict) -> str:
        backlog = metrics.get("backlog", 0)
        active = metrics.get("active_workers", 0)
        max_workers = metrics.get("max_workers", 0)
        baseline = metrics.get("baseline_workers", 0)
        autoscale_max = metrics.get("autoscale_max", max_workers)
        pending_stops = metrics.get("pending_stops", 0)
        backlog_high = metrics.get("backlog_high")
        backlog_low = metrics.get("backlog_low")
        hard_limit = metrics.get("backlog_hard_limit")
        shed_to = metrics.get("backlog_shed_to")
        dropped = metrics.get("dropped_tasks_total", 0)
        tasks_completed = metrics.get("tasks_completed", 0)
        avg_runtime = float(metrics.get("avg_runtime", 0.0))
        avg_wait = float(metrics.get("avg_wait_time", 0.0))
        ema_runtime = float(metrics.get("ema_runtime", 0.0))
        ema_wait = float(metrics.get("ema_wait_time", 0.0))
        last_runtime = float(metrics.get("last_runtime", 0.0))
        last_wait = float(metrics.get("last_wait_time", 0.0))
        longest_runtime = float(metrics.get("longest_runtime", 0.0))
        longest_wait = float(metrics.get("longest_wait", 0.0))
        lines = [
            f"Backlog: {backlog}",
            f"Workers: {active}/{max_workers} (baseline {baseline}, burst {autoscale_max})",
            f"Pending stops: {pending_stops}",
            f"Watermarks: high={backlog_high}, low={backlog_low}",
        ]
        if hard_limit is not None:
            lines.append(f"Hard limit: {hard_limit} -> shed to {shed_to}")
        lines.append(f"Dropped total: {dropped}")
        lines.append(
            "Task timings: "
            f"avg_run={avg_runtime:.2f}s (ema {ema_runtime:.2f}s), "
            f"avg_wait={avg_wait:.2f}s (ema {ema_wait:.2f}s)"
        )
        lines.append(
            "Last/peak: "
            f"last_run={last_runtime:.2f}s, last_wait={last_wait:.2f}s, "
            f"longest_run={longest_runtime:.2f}s, longest_wait={longest_wait:.2f}s"
        )
        lines.append(f"Tasks completed: {tasks_completed}")
        return "\n".join(lines)

    async def _emit_report(self, free_metrics: dict, accel_metrics: dict) -> None:
        backlog = int(free_metrics.get("backlog", 0))
        backlog_high = int(free_metrics.get("backlog_high") or 0)
        accel_backlog = int(accel_metrics.get("backlog", 0))
        ratio = backlog / backlog_high if backlog_high else 0

        dropped_total = int(free_metrics.get("dropped_tasks_total", 0))
        dropped_delta = max(0, dropped_total - self._last_dropped_total)
        self._last_dropped_total = dropped_total

        summary = [
            f"free_backlog={backlog}",
            f"free_workers={free_metrics.get('active_workers', 0)}/{free_metrics.get('max_workers', 0)}",
            f"accelerated_backlog={accel_backlog}",
            f"dropped_total={dropped_total}",
            f"avg_run={float(free_metrics.get('avg_runtime', 0.0)):.2f}s",
            f"avg_wait={float(free_metrics.get('avg_wait_time', 0.0)):.2f}s",
        ]
        if backlog_high:
            summary.append(f"backlog_ratio={ratio:.2f}")
        if dropped_delta:
            summary.append(f"dropped_since_last={dropped_delta}")

        print(f"[FreeQueueLag] {' '.join(summary)}")

        if backlog_high:
            description = f"Free backlog {backlog} (~{ratio:.2f}x high watermark)"
        else:
            description = f"Free backlog {backlog}"

        embed = discord.Embed(
            title="Free queue backlog warning",
            description=description,
            color=discord.Color.orange(),
        )
        embed.add_field(name="Free queue", value=self._format_metrics(free_metrics), inline=False)
        embed.add_field(name="Accelerated queue", value=self._format_metrics(accel_metrics), inline=False)
        embed.add_field(
            name="Current tuning snapshot",
            value="\n".join(
                [
                    f"FREE workers: base {self._config.free.max_workers} -> burst {self._config.free.autoscale_max}",
                    f"ACCEL workers: base {self._config.accelerated.max_workers} -> burst {self._config.accelerated.autoscale_max}",
                    f"Watermarks: high={self._config.autoscale.backlog_high}, low={self._config.autoscale.backlog_low}",
                ]
            ),
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

    async def _run(self) -> None:
        await self._bot.wait_until_ready()
        monitor_cfg = self._config.monitor
        try:
            while True:
                try:
                    await asyncio.sleep(monitor_cfg.check_interval)
                    if not self._free_queue.running:
                        continue

                    free_metrics = self._free_queue.metrics()
                    accel_metrics = self._accelerated_queue.metrics()

                    lagging = self._is_lagging(free_metrics, accel_metrics)
                    if lagging:
                        self._lag_hits += 1
                    else:
                        self._lag_hits = 0

                    if not lagging or self._lag_hits < monitor_cfg.required_hits:
                        continue

                    now = time.monotonic()
                    if self._last_report_at is not None and (now - self._last_report_at) < monitor_cfg.cooldown:
                        continue

                    await self._emit_report(free_metrics, accel_metrics)
                    self._last_report_at = now
                    self._lag_hits = 0
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    print(f"[FreeQueueLag] Monitor iteration failed: {exc}")
                    self._lag_hits = 0
        except asyncio.CancelledError:
            raise
