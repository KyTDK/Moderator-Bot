from __future__ import annotations

import asyncio
import builtins
import logging
import time
import traceback
from typing import Any, Optional

import discord

from ..instrumented_task import InstrumentedTask
from ..notifier import QueueEventNotifier
from ..types import SlowTaskReporter, TaskMetadata, TaskRuntimeDetail
from .events import QueueEventLogger
from .instrumentation import QueueInstrumentation
from .rate_tracker import RateTracker

__all__ = ["WorkerQueue"]

_SENTINEL = object()


class WorkerQueue:
    def __init__(
        self,
        max_workers: int = 3,
        *,
        autoscale_max: Optional[int] = None,
        backlog_high_watermark: int = 30,
        backlog_low_watermark: int = 5,
        autoscale_check_interval: float = 2.0,
        scale_down_grace: float = 5.0,
        name: Optional[str] = None,
        backlog_hard_limit: Optional[int] = 500,
        backlog_shed_to: Optional[int] = None,
        singular_task_reporter: Optional[SlowTaskReporter] = None,
        singular_runtime_threshold: Optional[float] = None,
        developer_log_bot: Optional[discord.Client] = None,
        developer_log_context: Optional[str] = None,
        developer_log_cooldown: float = 30.0,
        adaptive_mode: bool = False,
        rate_tracking_window: float = 180.0,
    ):
        self.queue = asyncio.Queue()
        self.max_workers = max_workers
        self._baseline_workers = max_workers
        self._autoscale_max = autoscale_max or max_workers

        self._backlog_high = backlog_high_watermark
        self._backlog_low = backlog_low_watermark
        self._check_interval = autoscale_check_interval
        self._scale_down_grace = scale_down_grace

        self._backlog_hard_limit = backlog_hard_limit
        self._backlog_shed_to = backlog_shed_to

        self._name = name or "queue"

        self.workers: list[asyncio.Task] = []
        self._busy_workers: int = 0
        self.running = False
        self._lock = asyncio.Lock()
        self._autoscaler_task: Optional[asyncio.Task] = None
        self._pending_stops: int = 0

        self._log = logging.getLogger(f"{__name__}.{self._name}")
        self._notifier = QueueEventNotifier(
            queue_name=self._name,
            logger=self._log,
            developer_bot=developer_log_bot,
            developer_context=developer_log_context,
            cooldown=developer_log_cooldown,
        )
        self._events = QueueEventLogger(name=self._name, notifier=self._notifier)

        if singular_runtime_threshold is None:
            singular_runtime_threshold = float(
                getattr(singular_task_reporter, "threshold", 30.0)
            )
        instrumentation_threshold = float(singular_runtime_threshold)
        self._instrumentation = QueueInstrumentation(
            queue_name=self._name,
            notifier=self._notifier,
            singular_task_reporter=singular_task_reporter,
            singular_runtime_threshold=instrumentation_threshold,
            slow_wait_threshold=15.0,
        )

        self._configured_autoscale_max: int = self._autoscale_max
        self._adaptive_step: int = 1
        self._adaptive_backlog_hits: int = 0
        self._adaptive_recovery_hits: int = 0
        self._adaptive_hit_threshold: int = 4
        self._adaptive_reset_hits: int = 12
        self._adaptive_bump_cooldown: float = 30.0
        self._last_adaptive_bump: float = 0.0
        self._adaptive_ceiling: int = 0

        self._adaptive_mode: bool = bool(adaptive_mode)
        self._rate_window: float = max(30.0, float(rate_tracking_window))
        self._arrival_tracker = RateTracker(window=self._rate_window)
        self._completion_tracker = RateTracker(window=self._rate_window)
        self._adaptive_plan_target: int = self.max_workers
        self._adaptive_plan_baseline: int = self._baseline_workers
        self._last_plan_applied: float = 0.0

        self._recompute_adaptive_ceiling()

    # ------------------------------------------------------------------ #
    # Lifecycle management
    # ------------------------------------------------------------------ #

    def _recompute_adaptive_ceiling(self) -> None:
        base_extra = max(1, self._baseline_workers)
        self._adaptive_ceiling = max(
            self._configured_autoscale_max + base_extra,
            self._configured_autoscale_max + 2,
        )

    def _active_workers(self) -> int:
        return sum(1 for w in self.workers if not w.done())

    async def start(self):
        async with self._lock:
            if self.running:
                return
            self.running = True
            for _ in range(self.max_workers):
                self.workers.append(asyncio.create_task(self.worker_loop()))
            if not self._adaptive_mode and self._autoscale_max > self._baseline_workers:
                self._autoscaler_task = asyncio.create_task(self.autoscaler_loop())

    async def stop(self):
        async with self._lock:
            if not self.running:
                return
            self.running = False
            if self._autoscaler_task is not None:
                self._autoscaler_task.cancel()
                try:
                    await self._autoscaler_task
                except asyncio.CancelledError:
                    pass
                finally:
                    self._autoscaler_task = None
            for _ in range(len(self.workers)):
                await self.queue.put(_SENTINEL)
            await asyncio.gather(*self.workers, return_exceptions=True)
            self.workers.clear()
            while not self.queue.empty():
                try:
                    self.queue.get_nowait()
                    self.queue.task_done()
                except asyncio.QueueEmpty:
                    break
            self._pending_stops = 0
        await self._instrumentation.wait_for_alerts()

    async def add_task(self, coro):
        if not asyncio.iscoroutine(coro):
            raise TypeError("add_task expects a coroutine")
        self._record_arrival()
        await self.queue.put(self._wrap_task(coro))
        await self._shed_backlog_if_needed(trigger="put")

    # ------------------------------------------------------------------ #
    # Worker sizing
    # ------------------------------------------------------------------ #

    async def resize_workers(self, new_max: int, *, reason: str | None = None):
        async with self._lock:
            if new_max == self.max_workers:
                return

            old_max = self.max_workers
            if new_max > self.max_workers:
                self.max_workers = new_max
                if self.running:
                    active = self._active_workers()
                    need = new_max - active
                    for _ in range(max(0, need)):
                        self.workers.append(asyncio.create_task(self.worker_loop()))
                self._events.scaled_up(old=old_max, new=new_max, reason=reason)
                return

            active = self._active_workers()
            deficit = max(0, active - new_max)
            to_stop = max(0, deficit - self._pending_stops)
            for _ in range(to_stop):
                await self.queue.put(_SENTINEL)
            self._pending_stops += to_stop
            self.max_workers = new_max
            self.workers = [w for w in self.workers if not w.done()]
        self._events.scaled_down(old=old_max, new=new_max, reason=reason)

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

    # ------------------------------------------------------------------ #
    # Autoscaling loop
    # ------------------------------------------------------------------ #

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

    # ------------------------------------------------------------------ #
    # Queue management helpers
    # ------------------------------------------------------------------ #

    async def _shed_backlog_if_needed(self, *, trigger: str) -> int:
        if self._backlog_hard_limit is None:
            return 0

        q = self.queue.qsize()
        if q <= self._backlog_hard_limit:
            return 0

        target = self._backlog_shed_to
        if target is None:
            target = self._backlog_high

        target = max(0, target)
        drop_n = max(0, q - target)
        if drop_n == 0:
            return 0

        dropped = 0
        returned_sentinels = 0

        async with self._lock:
            for _ in range(drop_n):
                try:
                    item = self.queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if item is _SENTINEL:
                    returned_sentinels += 1
                    self.queue.task_done()
                    continue
                self._close_enqueued_coroutine(item)
                dropped += 1
                self.queue.task_done()

            for _ in range(returned_sentinels):
                await self.queue.put(_SENTINEL)

        self._instrumentation.record_dropped(dropped)
        if dropped:
            self._notifier.warning(
                f"[WorkerQueue:{self._name}] Backlog {q} exceeded hard limit {self._backlog_hard_limit}; dropped {dropped} oldest task(s) (trigger={trigger}).",
                event_key="backlog_shed",
            )
        return dropped

    def _wrap_task(self, coro):
        loop = asyncio.get_running_loop()
        enqueued_at = loop.time()
        metadata = self._task_metadata(coro)
        backlog_at_enqueue = self.queue.qsize()

        return InstrumentedTask(self, coro, loop, enqueued_at, backlog_at_enqueue, metadata)

    def _task_metadata(self, coro) -> TaskMetadata:
        return TaskMetadata.from_coroutine(coro)

    def _record_wait(self, wait: float) -> None:
        self._instrumentation.record_wait(wait)

    def _record_runtime(self, detail: TaskRuntimeDetail) -> None:
        self._instrumentation.record_runtime(detail)

    def _handle_task_complete(self, detail: TaskRuntimeDetail, runtime: float, name: str) -> None:
        self._record_runtime(detail)
        self._record_completion()

    def _close_enqueued_coroutine(self, item) -> None:
        if isinstance(item, InstrumentedTask):
            item.close()
            return

        close = getattr(item, "close", None)
        if callable(close):
            try:
                close()
            except RuntimeError:
                pass
            except Exception:
                pass

    @staticmethod
    def _close_coroutine(coro) -> None:
        if coro is None:
            return
        close = getattr(coro, "close", None)
        if callable(close):
            try:
                close()
            except RuntimeError:
                pass
            except Exception:
                pass

    async def worker_loop(self):
        while True:
            task = await self.queue.get()
            if task is _SENTINEL:
                self.queue.task_done()
                if self._pending_stops > 0:
                    self._pending_stops -= 1
                break
            self._busy_workers += 1
            try:
                await task
            except asyncio.CancelledError:
                self._notifier.warning(
                    f"[WorkerQueue:{self._name}] Task cancelled.",
                    event_key="task_cancelled",
                )
            except BaseException as exc:  # noqa: BLE001
                base_group = getattr(builtins, "BaseExceptionGroup", None)
                if base_group is not None and isinstance(exc, base_group):
                    sub_exceptions = exc.exceptions  # type: ignore[attr-defined]
                    count = len(sub_exceptions)
                    self._notifier.error(
                        f"[WorkerQueue:{self._name}] Task group failed with {count} sub-exception(s).",
                        event_key="task_group_failure",
                    )
                    for idx, sub_exc in enumerate(sub_exceptions, start=1):
                        print(
                            f"[WorkerQueue:{self._name}] ├─ Sub-exception {idx}/{count}: {sub_exc!r}"
                        )
                        self._log.error(
                            "Sub-exception %s/%s: %r",
                            idx,
                            count,
                            sub_exc,
                            exc_info=(type(sub_exc), sub_exc, sub_exc.__traceback__),
                        )
                else:
                    self._notifier.error(
                        f"[WorkerQueue:{self._name}] Task failed: {exc!r}",
                        event_key="task_failure",
                    )
                    print(f"[WorkerQueue:{self._name}] Task failed: {exc!r}")
                    traceback.print_exception(type(exc), exc, exc.__traceback__)
            finally:
                if self._busy_workers > 0:
                    self._busy_workers -= 1
                self.queue.task_done()

    # ------------------------------------------------------------------ #
    # Metrics / telemetry accessors
    # ------------------------------------------------------------------ #

    def metrics(self) -> dict:
        payload = {
            "name": self._name,
            "running": self.running,
            "backlog": self.queue.qsize(),
            "active_workers": self._active_workers(),
            "busy_workers": self._busy_workers,
            "max_workers": self.max_workers,
            "baseline_workers": self._baseline_workers,
            "autoscale_max": self._autoscale_max,
            "pending_stops": self._pending_stops,
            "backlog_high": self._backlog_high,
            "backlog_low": self._backlog_low,
            "check_interval": self._check_interval,
            "scale_down_grace": self._scale_down_grace,
            "backlog_hard_limit": self._backlog_hard_limit,
            "backlog_shed_to": self._backlog_shed_to,
            "arrival_rate_per_min": self._arrival_tracker.rate_per_minute(),
            "completion_rate_per_min": self._completion_tracker.rate_per_minute(),
            "rate_tracking_window": self._rate_window,
            "adaptive_mode": self._adaptive_mode,
            "adaptive_target_workers": self._adaptive_plan_target,
            "adaptive_baseline_workers": self._adaptive_plan_baseline,
            "adaptive_last_applied": self._last_plan_applied,
        }
        payload.update(self._instrumentation.metrics_payload())
        return payload

    # ------------------------------------------------------------------ #
    # Internal utilities
    # ------------------------------------------------------------------ #

    def _record_arrival(self) -> None:
        self._arrival_tracker.record()

    def _record_completion(self) -> None:
        self._completion_tracker.record()

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
