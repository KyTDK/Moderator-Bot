from __future__ import annotations

import asyncio
import builtins
import logging
import time
import traceback
from typing import Any, Optional, Set

import discord

from .instrumented_task import InstrumentedTask
from .notifier import QueueEventNotifier
from .types import SlowTaskReporter, TaskMetadata, TaskRuntimeDetail

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
        slow_task_logging: bool = True,
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

        self._metrics_dropped: int = 0
        self._metrics_processed: int = 0
        self._metrics_total_runtime: float = 0.0
        self._metrics_total_wait: float = 0.0
        self._metrics_wait_samples: int = 0
        self._metrics_runtime_ema: Optional[float] = None
        self._metrics_wait_ema: Optional[float] = None
        self._metrics_last_runtime: Optional[float] = None
        self._metrics_last_wait: Optional[float] = None
        self._metrics_longest_runtime: float = 0.0
        self._metrics_longest_wait: float = 0.0
        self._metrics_last_runtime_detail: Optional[TaskRuntimeDetail] = None
        self._metrics_longest_runtime_detail: Optional[TaskRuntimeDetail] = None
        self._slow_wait_threshold: float = 15.0
        self._slow_runtime_threshold: float = 20.0
        self._slow_log_cooldown: float = 30.0
        self._last_wait_log: float = 0.0
        self._last_runtime_log: float = 0.0
        self._slow_task_logging: bool = slow_task_logging
        if singular_runtime_threshold is None:
            singular_runtime_threshold = float(
                getattr(singular_task_reporter, "threshold", 30.0)
            )
        self._singular_runtime_threshold: float = float(singular_runtime_threshold)
        self._singular_task_reporter = singular_task_reporter
        self._alert_tasks: Set[asyncio.Task[Any]] = set()

        self._configured_autoscale_max: int = self._autoscale_max
        self._adaptive_step: int = 1
        self._adaptive_backlog_hits: int = 0
        self._adaptive_recovery_hits: int = 0
        self._adaptive_hit_threshold: int = 4
        self._adaptive_reset_hits: int = 12
        self._adaptive_bump_cooldown: float = 30.0
        self._last_adaptive_bump: float = 0.0
        self._adaptive_ceiling: int = 0

        self._log = logging.getLogger(f"{__name__}.{self._name}")
        self._notifier = QueueEventNotifier(
            queue_name=self._name,
            logger=self._log,
            developer_bot=developer_log_bot,
            developer_context=developer_log_context,
            cooldown=developer_log_cooldown,
        )

        self._recompute_adaptive_ceiling()

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
            if self._autoscale_max > self._baseline_workers:
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
            if self._alert_tasks:
                await asyncio.gather(*self._alert_tasks, return_exceptions=True)
                self._alert_tasks.clear()

    async def add_task(self, coro):
        if not asyncio.iscoroutine(coro):
            raise TypeError("add_task expects a coroutine")
        await self.queue.put(self._wrap_task(coro))
        await self._shed_backlog_if_needed(trigger="put")

    async def resize_workers(self, new_max: int):
        async with self._lock:
            if new_max == self.max_workers:
                return

            if new_max > self.max_workers:
                self.max_workers = new_max
                if self.running:
                    active = self._active_workers()
                    need = new_max - active
                    for _ in range(max(0, need)):
                        self.workers.append(asyncio.create_task(self.worker_loop()))
                return

            active = self._active_workers()
            deficit = max(0, active - new_max)
            to_stop = max(0, deficit - self._pending_stops)
            for _ in range(to_stop):
                await self.queue.put(_SENTINEL)
            self._pending_stops += to_stop
            self.max_workers = new_max
            self.workers = [w for w in self.workers if not w.done()]

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
            await self.resize_workers(target_workers)

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
                    await self.resize_workers(self._autoscale_max)
                    low_since = None
                    self._notifier.info(
                        f"[WorkerQueue:{self._name}] Backlog {q} >= {self._backlog_high}, scaling up to {self._autoscale_max}",
                        event_key="scale_up_backlog",
                    )
                    continue

                wait_ema = float(self._metrics_wait_ema or 0.0)
                last_wait = float(self._metrics_last_wait or 0.0)
                wait_signal = max(wait_ema, last_wait)
                if (
                    q > 0
                    and wait_signal >= self._slow_wait_threshold
                    and self.max_workers < self._autoscale_max
                ):
                    await self.resize_workers(self._autoscale_max)
                    low_since = None
                    self._notifier.warning(
                        f"[WorkerQueue:{self._name}] Wait {wait_signal:.2f}s >= {self._slow_wait_threshold:.2f}s, scaling up to {self._autoscale_max}",
                        event_key="scale_up_wait",
                    )
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
                                await self.resize_workers(self._autoscale_max)
                                self._notifier.info(
                                    f"[WorkerQueue:{self._name}] Sustained backlog detected; increasing autoscale_max to {self._autoscale_max}",
                                    event_key="adaptive_bump",
                                )
                                continue

                over_baseline = max(0, active - self._baseline_workers - self._pending_stops)
                if q <= self._backlog_low and over_baseline > 0:
                    now = loop.time()
                    if low_since is None:
                        low_since = now
                    elif (now - low_since) >= self._scale_down_grace:
                        await self.resize_workers(self._baseline_workers)
                        self._notifier.info(
                            f"[WorkerQueue:{self._name}] Backlog stable (<= {self._backlog_low}), scaling down to baseline {self._baseline_workers}",
                            event_key="scale_down",
                        )
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
                            await self.resize_workers(self._autoscale_max)
                        self._notifier.info(
                            f"[WorkerQueue:{self._name}] Backlog eased; restoring autoscale_max to {self._autoscale_max}",
                            event_key="adaptive_reset",
                        )
        except asyncio.CancelledError:
            pass

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

        self._metrics_dropped += dropped
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
        self._metrics_last_wait = wait
        self._metrics_total_wait += wait
        self._metrics_wait_samples += 1
        if self._metrics_wait_ema is None:
            self._metrics_wait_ema = wait
        else:
            self._metrics_wait_ema = (self._metrics_wait_ema * 0.8) + (wait * 0.2)
        if wait > self._metrics_longest_wait:
            self._metrics_longest_wait = wait

    def _record_runtime(self, detail: TaskRuntimeDetail) -> None:
        runtime = detail.runtime
        self._metrics_processed += 1
        self._metrics_last_runtime = runtime
        self._metrics_total_runtime += runtime
        if self._metrics_runtime_ema is None:
            self._metrics_runtime_ema = runtime
        else:
            self._metrics_runtime_ema = (self._metrics_runtime_ema * 0.8) + (runtime * 0.2)
        if runtime > self._metrics_longest_runtime:
            self._metrics_longest_runtime = runtime
        self._metrics_last_runtime_detail = detail
        if runtime >= self._metrics_longest_runtime:
            self._metrics_longest_runtime_detail = detail
        self._maybe_report_singular_task(detail)

    def _maybe_log_wait(self, wait: float, backlog: int, name: str) -> None:
        if not self._slow_task_logging:
            return
        now = time.monotonic()
        if now - self._last_wait_log < self._slow_log_cooldown:
            return
        self._last_wait_log = now
        message = (
            f"[WorkerQueue:{self._name}] Task {name!r} waited {wait:.2f}s before starting "
            f"(backlog_at_enqueue={backlog}, current_backlog={self.queue.qsize()}, workers={self._active_workers()}/{self.max_workers})"
        )
        self._notifier.warning(message, event_key="slow_wait")

    def _maybe_log_runtime(self, runtime: float, name: str) -> None:
        return

    def _handle_task_complete(self, detail: TaskRuntimeDetail, runtime: float, name: str) -> None:
        self._record_runtime(detail)
        if runtime > self._slow_runtime_threshold:
            self._maybe_log_runtime(runtime, name)

    def _maybe_report_singular_task(self, detail: TaskRuntimeDetail) -> None:
        reporter = self._singular_task_reporter
        if reporter is None:
            return
        if detail.runtime < self._singular_runtime_threshold:
            return
        if not self._is_singular(detail):
            return
        self._schedule_singular_task_alert(reporter, detail)

    @staticmethod
    def _is_singular(detail: TaskRuntimeDetail) -> bool:
        return detail.max_workers <= 1 and detail.autoscale_max <= 1

    def _schedule_singular_task_alert(
        self,
        reporter: SlowTaskReporter,
        detail: TaskRuntimeDetail,
    ) -> None:
        try:
            task = asyncio.create_task(reporter(detail, self._name))
        except RuntimeError:
            self._notifier.warning(
                f"[WorkerQueue:{self._name}] Unable to schedule singular task alert; no running event loop.",
                event_key="singular_schedule_failure",
            )
            return
        self._alert_tasks.add(task)
        task.add_done_callback(self._on_alert_task_done)

    def _on_alert_task_done(self, task: asyncio.Task[Any]) -> None:
        self._alert_tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception as exc:  # noqa: BLE001
            self._notifier.error(
                f"[WorkerQueue:{self._name}] Singular task reporter failed: {exc!r}",
                event_key="singular_reporter_failure",
            )

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

    def metrics(self) -> dict:
        return {
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
            "dropped_tasks_total": self._metrics_dropped,
            "backlog_hard_limit": self._backlog_hard_limit,
            "backlog_shed_to": self._backlog_shed_to,
            "tasks_completed": self._metrics_processed,
            "avg_runtime": (self._metrics_total_runtime / self._metrics_processed) if self._metrics_processed else 0.0,
            "avg_wait_time": (self._metrics_total_wait / self._metrics_wait_samples) if self._metrics_wait_samples else 0.0,
            "ema_runtime": self._metrics_runtime_ema or 0.0,
            "ema_wait_time": self._metrics_wait_ema or 0.0,
            "last_runtime": self._metrics_last_runtime or 0.0,
            "last_wait_time": self._metrics_last_wait or 0.0,
            "longest_runtime": self._metrics_longest_runtime,
            "longest_wait": self._metrics_longest_wait,
            "last_runtime_details": self._metrics_last_runtime_detail.as_mapping() if self._metrics_last_runtime_detail else {},
            "longest_runtime_details": self._metrics_longest_runtime_detail.as_mapping() if self._metrics_longest_runtime_detail else {},
        }
