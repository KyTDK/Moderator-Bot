from __future__ import annotations

import asyncio
from typing import Any, Optional, Set

from ..notifier import QueueEventNotifier
from ..types import SlowTaskReporter, TaskRuntimeDetail

__all__ = ["QueueInstrumentation"]


class QueueInstrumentation:
    """Capture worker queue metrics and singular task reporting."""

    def __init__(
        self,
        *,
        queue_name: str,
        notifier: QueueEventNotifier,
        singular_task_reporter: Optional[SlowTaskReporter],
        singular_runtime_threshold: float,
        slow_wait_threshold: float,
    ) -> None:
        self._queue_name = queue_name
        self._notifier = notifier
        self._singular_task_reporter = singular_task_reporter
        self._singular_runtime_threshold = float(singular_runtime_threshold)
        self._slow_wait_threshold = float(slow_wait_threshold)
        self._alert_tasks: Set[asyncio.Task[Any]] = set()

        self._dropped: int = 0
        self._processed: int = 0
        self._total_runtime: float = 0.0
        self._total_wait: float = 0.0
        self._wait_samples: int = 0
        self._runtime_ema: Optional[float] = None
        self._wait_ema: Optional[float] = None
        self._last_runtime: Optional[float] = None
        self._last_wait: Optional[float] = None
        self._longest_runtime: float = 0.0
        self._longest_wait: float = 0.0
        self._last_runtime_detail: Optional[TaskRuntimeDetail] = None
        self._longest_runtime_detail: Optional[TaskRuntimeDetail] = None

    # --------------------------------------------------------------------- #
    # Public interface
    # --------------------------------------------------------------------- #

    def reset(self) -> None:
        self._dropped = 0
        self._processed = 0
        self._total_runtime = 0.0
        self._total_wait = 0.0
        self._wait_samples = 0
        self._runtime_ema = None
        self._wait_ema = None
        self._last_runtime = None
        self._last_wait = None
        self._longest_runtime = 0.0
        self._longest_wait = 0.0
        self._last_runtime_detail = None
        self._longest_runtime_detail = None

    def record_wait(self, wait: float) -> None:
        self._last_wait = wait
        self._total_wait += wait
        self._wait_samples += 1

        if self._wait_ema is None:
            self._wait_ema = wait
        else:
            self._wait_ema = (self._wait_ema * 0.8) + (wait * 0.2)

        if wait > self._longest_wait:
            self._longest_wait = wait

    def record_runtime(self, detail: TaskRuntimeDetail) -> None:
        runtime = detail.runtime
        self._processed += 1
        self._last_runtime = runtime
        self._total_runtime += runtime

        if self._runtime_ema is None:
            self._runtime_ema = runtime
        else:
            self._runtime_ema = (self._runtime_ema * 0.8) + (runtime * 0.2)

        if runtime > self._longest_runtime:
            self._longest_runtime = runtime

        self._last_runtime_detail = detail
        if runtime >= self._longest_runtime:
            self._longest_runtime_detail = detail

        self._maybe_report_singular_task(detail)

    def record_dropped(self, count: int) -> None:
        self._dropped += count

    async def wait_for_alerts(self) -> None:
        if not self._alert_tasks:
            return
        await asyncio.gather(*self._alert_tasks, return_exceptions=True)
        self._alert_tasks.clear()

    # ------------------------------------------------------------------ #
    # Metrics snapshots / derived properties
    # ------------------------------------------------------------------ #

    @property
    def wait_ema(self) -> float:
        return float(self._wait_ema or 0.0)

    @property
    def last_wait(self) -> float:
        return float(self._last_wait or 0.0)

    @property
    def slow_wait_threshold(self) -> float:
        return self._slow_wait_threshold

    def metrics_payload(self) -> dict[str, Any]:
        return {
            "dropped_tasks_total": self._dropped,
            "tasks_completed": self._processed,
            "avg_runtime": (self._total_runtime / self._processed) if self._processed else 0.0,
            "avg_wait_time": (self._total_wait / self._wait_samples) if self._wait_samples else 0.0,
            "ema_runtime": self._runtime_ema or 0.0,
            "ema_wait_time": self._wait_ema or 0.0,
            "last_runtime": self._last_runtime or 0.0,
            "last_wait_time": self._last_wait or 0.0,
            "longest_runtime": self._longest_runtime,
            "longest_wait": self._longest_wait,
            "last_runtime_details": self._last_runtime_detail.as_mapping() if self._last_runtime_detail else {},
            "longest_runtime_details": self._longest_runtime_detail.as_mapping() if self._longest_runtime_detail else {},
        }

    # ------------------------------------------------------------------ #
    # Singular task handling
    # ------------------------------------------------------------------ #

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
            task = asyncio.create_task(reporter(detail, self._queue_name))
        except RuntimeError:
            self._notifier.warning(
                f"[WorkerQueue:{self._queue_name}] Unable to schedule singular task alert; no running event loop.",
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
                f"[WorkerQueue:{self._queue_name}] Singular task reporter failed: {exc!r}",
                event_key="singular_reporter_failure",
            )
