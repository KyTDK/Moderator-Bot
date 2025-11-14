from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Optional, Tuple

from modules.nsfw_scanner.constants import LOG_CHANNEL_ID
from modules.utils.log_channel import send_developer_log_embed

from .config import AggregatedModerationConfig
from .media_rates import MediaProcessingRate, MediaRateCalculator
from .performance_alerts import build_performance_alert_embed
from .queue_snapshot import QueueSnapshot


@dataclass(slots=True)
class PerformanceComparison:
    runtime_ratio: float
    wait_ratio: float
    free_runtime: float
    accel_runtime: float
    free_wait: float
    accel_wait: float
    reasons: list[str]


class AcceleratedPerformanceMonitor:
    """Watches accelerated queue health relative to the free/core queue."""

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
        self._config = config.performance_monitor

        self._rate_calculator = MediaRateCalculator()
        self._task: Optional[asyncio.Task] = None
        self._hits: int = 0
        self._last_report_at: Optional[float] = None

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        loop = asyncio.get_running_loop()
        self._task = loop.create_task(self._run(), name="accelerated_performance_monitor")

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
            self._hits = 0
            self._last_report_at = None

    async def _run(self) -> None:
        await self._bot.wait_until_ready()
        cfg = self._config
        try:
            while True:
                try:
                    await asyncio.sleep(cfg.check_interval)
                    if not (self._free_queue.running and self._accelerated_queue.running):
                        continue

                    free_snapshot = QueueSnapshot.from_mapping(self._free_queue.metrics())
                    accel_snapshot = QueueSnapshot.from_mapping(self._accelerated_queue.metrics())

                    comparison = self._evaluate_comparison(free_snapshot, accel_snapshot)
                    if comparison is None:
                        self._hits = 0
                        continue

                    self._hits += 1
                    if self._hits < cfg.required_hits:
                        continue

                    now = time.monotonic()
                    if self._last_report_at is not None and (now - self._last_report_at) < cfg.cooldown:
                        continue

                    await self._emit_report(free_snapshot, accel_snapshot, comparison)
                    self._last_report_at = now
                    self._hits = 0
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    print(f"[AcceleratedPerformance] Monitor iteration failed: {exc}")
                    self._hits = 0
        except asyncio.CancelledError:
            raise

    def _evaluate_comparison(
        self,
        free: QueueSnapshot,
        accel: QueueSnapshot,
    ) -> Optional[PerformanceComparison]:
        cfg = self._config
        if min(free.tasks_completed, accel.tasks_completed) < cfg.min_completed_tasks:
            return None

        free_runtime = self._resolve_runtime(free)
        accel_runtime = self._resolve_runtime(accel)
        if free_runtime < cfg.min_runtime_seconds or accel_runtime < cfg.min_runtime_seconds:
            return None

        free_wait = self._resolve_wait(free)
        accel_wait = self._resolve_wait(accel)

        runtime_ratio = accel_runtime / max(free_runtime, 0.001)
        wait_ratio = accel_wait / max(free_wait, 0.001) if free_wait >= cfg.min_runtime_seconds else 0.0

        reasons: list[str] = []
        if runtime_ratio >= cfg.ratio_threshold:
            reasons.append(
                f"Runtime ratio {runtime_ratio:.2f} ({accel_runtime:.2f}s accel vs {free_runtime:.2f}s free)."
            )
        if accel_runtime >= free_runtime:
            reasons.append("Accelerated avg runtime is now slower or equal to the core path.")
        if wait_ratio >= cfg.wait_ratio_threshold:
            reasons.append(
                f"Wait ratio {wait_ratio:.2f} ({accel_wait:.2f}s accel vs {free_wait:.2f}s free)."
            )

        if not reasons:
            return None

        return PerformanceComparison(
            runtime_ratio=runtime_ratio,
            wait_ratio=wait_ratio,
            free_runtime=free_runtime,
            accel_runtime=accel_runtime,
            free_wait=free_wait,
            accel_wait=accel_wait,
            reasons=reasons,
        )

    @staticmethod
    def _resolve_runtime(snapshot: QueueSnapshot) -> float:
        for value in (
            snapshot.avg_runtime,
            snapshot.ema_runtime,
            snapshot.last_runtime,
            snapshot.longest_runtime,
        ):
            if value and value > 0:
                return float(value)
        return 0.0

    @staticmethod
    def _resolve_wait(snapshot: QueueSnapshot) -> float:
        for value in (
            snapshot.avg_wait,
            snapshot.ema_wait,
            snapshot.last_wait,
            snapshot.longest_wait,
        ):
            if value and value > 0:
                return float(value)
        return 0.0

    async def _emit_report(
        self,
        free: QueueSnapshot,
        accel: QueueSnapshot,
        comparison: PerformanceComparison,
    ) -> None:
        rates, rate_summary = await self._collect_rates_summary()

        summary = [
            "[AcceleratedPerformance]",
            f"runtime_ratio={comparison.runtime_ratio:.2f}",
            f"wait_ratio={comparison.wait_ratio:.2f}",
            f"accel_avg={comparison.accel_runtime:.2f}s",
            f"free_avg={comparison.free_runtime:.2f}s",
            f"accel_wait={comparison.accel_wait:.2f}s",
            f"free_wait={comparison.free_wait:.2f}s",
            f"reasons={' | '.join(comparison.reasons)}",
            f"rates={rate_summary}",
        ]
        print(" ".join(summary))

        if not LOG_CHANNEL_ID:
            return

        embed = build_performance_alert_embed(
            free=free,
            accel=accel,
            comparison=comparison,
            rates=rates,
            calculator=self._rate_calculator,
        )
        success = await send_developer_log_embed(
            self._bot,
            embed=embed,
            context="accelerated_performance",
        )
        if not success:
            print(f"[AcceleratedPerformance] Failed to send alert to LOG_CHANNEL_ID={LOG_CHANNEL_ID}")

    async def _collect_rates_summary(self) -> Tuple[list[MediaProcessingRate], str]:
        try:
            rates = await self._rate_calculator.compute_rates()
        except Exception as exc:  # noqa: BLE001
            print(f"[AcceleratedPerformance] Failed to build rate summary: {exc}")
            return [], "unavailable"

        summary = ", ".join(rate.format_console(self._rate_calculator.window_minutes) for rate in rates) or "no data"
        return rates, summary


__all__ = ["AcceleratedPerformanceMonitor"]
