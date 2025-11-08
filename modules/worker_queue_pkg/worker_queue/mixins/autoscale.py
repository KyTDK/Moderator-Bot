from __future__ import annotations

import asyncio
import time
from typing import Optional

__all__ = ["AutoscaleMixin"]


class AutoscaleMixin:
    """Adaptive autoscaling support for the worker queue."""

    def _recompute_adaptive_ceiling(self) -> None:
        base_extra = max(1, self._baseline_workers)
        self._adaptive_ceiling = max(
            self._configured_autoscale_max + base_extra,
            self._configured_autoscale_max + 2,
        )

    async def ensure_capacity(self, target_workers: int):
        target_workers = max(1, int(target_workers))
        needs_resize = False

        async with self._lock:
            if target_workers > self._autoscale_max:
                self._autoscale_max = target_workers
                if self._autoscale_max > self._configured_autoscale_max:
                    self._configured_autoscale_max = self._autoscale_max
                    self._recompute_adaptive_ceiling()
            if target_workers > self.max_workers:
                needs_resize = True

        if needs_resize:
            await self.resize_workers(target_workers, reason="ensure_capacity")

    async def update_adaptive_plan(
        self,
        *,
        target_workers: int,
        baseline_workers: Optional[int] = None,
        backlog_high: Optional[int] = None,
        backlog_low: Optional[int] = None,
        backlog_hard_limit: Optional[int] = None,
        backlog_shed_to: Optional[int] = None,
    ) -> None:
        if not self._adaptive_mode:
            return

        target = max(1, int(target_workers))
        baseline = baseline_workers if baseline_workers is not None else target
        baseline = max(1, min(int(baseline), target))

        before_state = {
            "target": self._adaptive_plan_target,
            "baseline": self._adaptive_plan_baseline,
            "max_workers": self.max_workers,
            "autoscale_max": self._autoscale_max,
            "backlog_high": self._backlog_high,
            "backlog_low": self._backlog_low,
            "backlog_hard": self._backlog_hard_limit,
            "backlog_shed_to": self._backlog_shed_to,
        }

        async with self._lock:
            self._adaptive_plan_target = target
            self._adaptive_plan_baseline = baseline
            self._baseline_workers = baseline
            if backlog_high is not None:
                self._backlog_high = int(backlog_high)
            if backlog_low is not None:
                self._backlog_low = int(backlog_low)
            if backlog_hard_limit is not None:
                self._backlog_hard_limit = int(backlog_hard_limit)
            if backlog_shed_to is not None:
                self._backlog_shed_to = int(backlog_shed_to)
            self._configured_autoscale_max = target
            self._autoscale_max = target
            self._recompute_adaptive_ceiling()
            current_max = self.max_workers

        if current_max != target:
            await self.resize_workers(target, reason="adaptive_plan")
        self._last_plan_applied = time.monotonic()

        after_state = {
            "target": self._adaptive_plan_target,
            "baseline": self._adaptive_plan_baseline,
            "max_workers": self.max_workers,
            "autoscale_max": self._autoscale_max,
            "backlog_high": self._backlog_high,
            "backlog_low": self._backlog_low,
            "backlog_hard": self._backlog_hard_limit,
            "backlog_shed_to": self._backlog_shed_to,
        }
        changes = self._summarize_plan_changes(before_state, after_state)
        if changes:
            self._events.adaptive_plan_updated(
                changes=changes,
                target=self._adaptive_plan_target,
                baseline=self._adaptive_plan_baseline,
                backlog_high=self._backlog_high,
            )

    async def autoscaler_loop(self):
        low_since: Optional[float] = None
        loop = asyncio.get_running_loop()
        try:
            while self.running:
                await asyncio.sleep(self._check_interval)
                q = self.queue.qsize()
                self.workers = [w for w in self.workers if not w.done()]
                active = self._active_workers()

                await self._shed_backlog_if_needed(trigger="autoscaler")

                if q >= self._backlog_high and active < self._autoscale_max:
                    await self.resize_workers(self._autoscale_max, reason="autoscaler_backlog_high")
                    low_since = None
                    continue

                wait_ema = self._instrumentation.wait_ema
                last_wait = self._instrumentation.last_wait
                wait_signal = max(wait_ema, last_wait)
                if (
                    q > 0
                    and wait_signal >= self._instrumentation.slow_wait_threshold
                    and self.max_workers < self._autoscale_max
                ):
                    await self.resize_workers(self._autoscale_max, reason="autoscaler_wait_pressure")
                    low_since = None
                    continue

                if self._autoscale_max < self._adaptive_ceiling:
                    busy = self._busy_workers
                    saturated = busy >= self.max_workers
                    if q >= self._backlog_high and saturated:
                        self._adaptive_backlog_hits += 1
                    else:
                        self._adaptive_backlog_hits = 0
                    if self._adaptive_backlog_hits >= self._adaptive_hit_threshold:
                        now = loop.time()
                        if (now - self._last_adaptive_bump) >= self._adaptive_bump_cooldown:
                            new_limit = min(
                                self._autoscale_max + self._adaptive_step,
                                self._adaptive_ceiling,
                            )
                            if new_limit > self._autoscale_max:
                                self._autoscale_max = new_limit
                                self._last_adaptive_bump = now
                                self._adaptive_backlog_hits = 0
                                self._adaptive_recovery_hits = 0
                                await self.resize_workers(self._autoscale_max, reason="autoscaler_adaptive_ceiling")
                                continue

                over_baseline = max(0, active - self._baseline_workers - self._pending_stops)
                if q <= self._backlog_low and over_baseline > 0:
                    now = loop.time()
                    if low_since is None:
                        low_since = now
                    elif (now - low_since) >= self._scale_down_grace:
                        await self.resize_workers(self._baseline_workers, reason="autoscaler_scale_down")
                        low_since = None
                else:
                    low_since = None

                if self._autoscale_max > self._configured_autoscale_max:
                    recovery_floor = self._backlog_low if self._backlog_low is not None else 0
                    if q <= max(recovery_floor, 1):
                        self._adaptive_recovery_hits += 1
                    else:
                        self._adaptive_recovery_hits = 0
                    if self._adaptive_recovery_hits >= self._adaptive_reset_hits:
                        self._autoscale_max = self._configured_autoscale_max
                        self._last_adaptive_bump = 0.0
                        self._adaptive_backlog_hits = 0
                        self._adaptive_recovery_hits = 0
                        self._recompute_adaptive_ceiling()
                        if self.max_workers > self._autoscale_max:
                            await self.resize_workers(self._autoscale_max, reason="autoscaler_ceiling_reset")
        except asyncio.CancelledError:
            pass

    @staticmethod
    def _summarize_plan_changes(
        before: dict[str, Optional[int]],
        after: dict[str, Optional[int]],
    ) -> list[str]:
        fields = [
            ("target", "target"),
            ("baseline", "baseline"),
            ("max_workers", "max"),
            ("autoscale_max", "ceiling"),
            ("backlog_high", "backlog_high"),
            ("backlog_low", "backlog_low"),
            ("backlog_hard", "hard_limit"),
            ("backlog_shed_to", "shed_to"),
        ]
        changes: list[str] = []
        for key, label in fields:
            old = before.get(key)
            new = after.get(key)
            if old == new:
                continue
            changes.append(f"{label} {old}->{new}")
        return changes
