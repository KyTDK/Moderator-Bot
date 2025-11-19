from __future__ import annotations

import asyncio
import time
from typing import Optional

from modules.nsfw_scanner.constants import LOG_CHANNEL_ID
from modules.utils.log_channel import send_developer_log_embed

from .config import AggregatedModerationConfig
from .alert_payloads import (
    build_backlog_cleared_embed,
    build_backlog_embed,
    build_video_backlog_cleared_embed,
    build_video_backlog_embed,
)
from .media_rates import MediaProcessingRate, MediaRateCalculator
from .queue_snapshot import QueueSnapshot


class FreeQueueMonitor:
    """Observes queue health and surfaces lag for operators."""

    def __init__(
        self,
        *,
        bot,
        free_queue,
        accelerated_queue,
        video_queue,
        config: AggregatedModerationConfig,
    ) -> None:
        self._bot = bot
        self._free_queue = free_queue
        self._accelerated_queue = accelerated_queue
        self._video_queue = video_queue
        self._config = config

        self._task: Optional[asyncio.Task] = None
        self._lag_hits: int = 0
        self._last_report_at: Optional[float] = None
        self._last_dropped_total: int = 0
        self._rate_calculator = MediaRateCalculator()
        self._backlog_active: bool = False
        self._video_lag_hits: int = 0
        self._video_last_report_at: Optional[float] = None
        self._video_backlog_active: bool = False

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
            self._backlog_active = False
            self._video_lag_hits = 0
            self._video_last_report_at = None
            self._video_backlog_active = False

    @staticmethod
    def _is_lagging(free: QueueSnapshot, accel: QueueSnapshot) -> bool:
        backlog_pressure = free.backlog_high is not None and free.backlog >= free.backlog_high
        saturation = free.backlog > 0 and free.busy_workers >= free.max_workers
        high_ratio = free.backlog_high is not None and free.backlog >= int(free.backlog_high * 1.5)
        relative_gap = (free.backlog - accel.backlog) >= max(5, accel.max_workers)
        backlog_growth = free.backlog >= free.max_workers * 4
        near_hard_limit = free.backlog_hard_limit is not None and free.backlog >= max(free.backlog_hard_limit - 5, 0)
        burst_saturated = free.backlog > free.autoscale_max and free.busy_workers >= free.max_workers

        return backlog_pressure and (
            saturation
            or high_ratio
            or relative_gap
            or backlog_growth
            or near_hard_limit
            or burst_saturated
        )

    async def _emit_report(self, free: QueueSnapshot, accel: QueueSnapshot) -> None:
        dropped_delta = max(0, free.dropped_total - self._last_dropped_total)
        self._last_dropped_total = free.dropped_total

        rates, rate_summary = await self._collect_rates_summary()

        summary = [
            f"free_backlog={free.backlog}",
            f"free_busy={free.busy_workers}/{free.max_workers}",
            f"free_workers={free.active_workers}/{free.max_workers}",
            f"accelerated_backlog={accel.backlog}",
            f"dropped_total={free.dropped_total}",
            f"avg_run={free.avg_runtime:.2f}s",
            f"avg_wait={free.avg_wait:.2f}s",
            f"processing_rates=[{rate_summary}]",
        ]
        if free.backlog_high:
            summary.append(f"backlog_ratio={free.backlog_ratio:.2f}")
        if dropped_delta:
            summary.append(f"dropped_since_last={dropped_delta}")

        print(f"[FreeQueueLag] {' '.join(summary)}")

        if not LOG_CHANNEL_ID:
            return

        embed = build_backlog_embed(
            free=free,
            accel=accel,
            dropped_delta=dropped_delta,
            rates=rates,
            calculator=self._rate_calculator,
        )
        if not await send_developer_log_embed(
            self._bot,
            embed=embed,
            context="free_queue_backlog",
        ):
            print(f"[FreeQueueLag] Failed to send backlog warning to LOG_CHANNEL_ID={LOG_CHANNEL_ID}")
            return

        self._backlog_active = True

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

                    if self._video_queue and self._video_queue.running:
                        video_snapshot = QueueSnapshot.from_mapping(self._video_queue.metrics())
                        await self._handle_video_queue(video_snapshot, monitor_cfg)

                    if self._is_lagging(free_snapshot, accel_snapshot):
                        self._lag_hits += 1
                    else:
                        if self._backlog_active and free_snapshot.backlog_recovered():
                            await self._emit_backlog_cleared(free_snapshot, accel_snapshot)
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

    @staticmethod
    def _is_video_lagging(snapshot: QueueSnapshot) -> bool:
        if snapshot.backlog <= 0:
            return False
        backlog_pressure = snapshot.backlog_high is not None and snapshot.backlog >= snapshot.backlog_high
        wait_signal = snapshot.wait_signal()
        wait_pressure = wait_signal >= 15.0
        worker_saturated = snapshot.busy_workers >= snapshot.max_workers and snapshot.backlog > 0
        burst_pressure = snapshot.backlog >= max(snapshot.max_workers * 3, snapshot.autoscale_max or snapshot.max_workers)
        return backlog_pressure or (worker_saturated and wait_pressure) or burst_pressure

    async def _handle_video_queue(self, snapshot: QueueSnapshot, monitor_cfg) -> None:
        if self._is_video_lagging(snapshot):
            self._video_lag_hits += 1
        else:
            if self._video_backlog_active and snapshot.backlog_recovered():
                await self._emit_video_backlog_cleared(snapshot)
            self._video_lag_hits = 0

        if self._video_lag_hits < monitor_cfg.required_hits:
            return

        now = time.monotonic()
        if self._video_last_report_at is not None and (now - self._video_last_report_at) < monitor_cfg.cooldown:
            return

        await self._emit_video_report(snapshot)
        self._video_last_report_at = now
        self._video_lag_hits = 0

    async def _emit_backlog_cleared(
        self,
        free: QueueSnapshot,
        accel: QueueSnapshot,
    ) -> None:
        rates, rate_summary = await self._collect_rates_summary()

        summary = [
            "backlog_recovered",
            f"free_backlog={free.backlog}",
            f"accelerated_backlog={accel.backlog}",
            f"processing_rates=[{rate_summary}]",
        ]
        print(f"[FreeQueueLag] {' '.join(summary)}")

        if not LOG_CHANNEL_ID:
            self._backlog_active = False
            return

        embed = build_backlog_cleared_embed(
            free=free,
            accel=accel,
            rates=rates,
            calculator=self._rate_calculator,
        )
        if not await send_developer_log_embed(
            self._bot,
            embed=embed,
            context="free_queue_backlog",
        ):
            print(f"[FreeQueueLag] Failed to send backlog recovery notice to LOG_CHANNEL_ID={LOG_CHANNEL_ID}")
            return

        self._backlog_active = False

    async def _emit_video_report(self, video: QueueSnapshot) -> None:
        summary = [
            "[VideoQueueLag]",
            f"backlog={video.backlog}",
            f"busy={video.busy_workers}/{video.max_workers}",
            f"avg_wait={video.avg_wait:.2f}s",
            f"avg_run={video.avg_runtime:.2f}s",
        ]
        print(" ".join(summary))

        if LOG_CHANNEL_ID:
            embed = build_video_backlog_embed(video=video)
            success = await send_developer_log_embed(
                self._bot,
                embed=embed,
                context="accelerated_video_backlog",
            )
            if not success:
                print(f"[VideoQueueLag] Failed to send backlog warning to LOG_CHANNEL_ID={LOG_CHANNEL_ID}")
        self._video_backlog_active = True

    async def _emit_video_backlog_cleared(self, video: QueueSnapshot) -> None:
        print(f"[VideoQueueLag] backlog_recovered backlog={video.backlog}")
        if LOG_CHANNEL_ID:
            embed = build_video_backlog_cleared_embed(video=video)
            success = await send_developer_log_embed(
                self._bot,
                embed=embed,
                context="accelerated_video_backlog",
            )
            if not success:
                print(f"[VideoQueueLag] Failed to send backlog recovery notice to LOG_CHANNEL_ID={LOG_CHANNEL_ID}")
        self._video_backlog_active = False

    async def _collect_rates_summary(self) -> tuple[list[MediaProcessingRate], str]:
        rates = await self._rate_calculator.compute_rates()
        window_minutes = self._rate_calculator.window_minutes
        if rates:
            rate_summary = ", ".join(rate.format_console(window_minutes) for rate in rates)
        else:
            rate_summary = "none"
        return rates, rate_summary


__all__ = ["FreeQueueMonitor"]
