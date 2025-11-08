from __future__ import annotations

import asyncio
import builtins
import traceback
from typing import Any, Optional

from ...instrumented_task import InstrumentedTask
from ...types import TaskMetadata
from ..constants import SENTINEL

__all__ = ["LifecycleMixin"]


class LifecycleMixin:
    """Manages worker lifecycle, task orchestration, and queue operations."""

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
                await self.queue.put(SENTINEL)
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

    async def resize_workers(self, new_max: int, *, reason: Optional[str] = None):
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
                await self.queue.put(SENTINEL)
            self._pending_stops += to_stop
            self.max_workers = new_max
            self.workers = [w for w in self.workers if not w.done()]
        self._events.scaled_down(old=old_max, new=new_max, reason=reason)

    def _wrap_task(self, coro):
        loop = asyncio.get_running_loop()
        enqueued_at = loop.time()
        metadata = self._task_metadata(coro)
        backlog_at_enqueue = self.queue.qsize()
        return InstrumentedTask(self, coro, loop, enqueued_at, backlog_at_enqueue, metadata)

    def _task_metadata(self, coro) -> TaskMetadata:
        return TaskMetadata.from_coroutine(coro)

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
            if task is SENTINEL:
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
                    details={"Task": repr(task)},
                )
            except BaseException as exc:  # noqa: BLE001
                base_group = getattr(builtins, "BaseExceptionGroup", None)
                if base_group is not None and isinstance(exc, base_group):
                    sub_exceptions = exc.exceptions  # type: ignore[attr-defined]
                    count = len(sub_exceptions)
                    self._notifier.error(
                        f"[WorkerQueue:{self._name}] Task group failed with {count} sub-exception(s).",
                        event_key="task_group_failure",
                        details={
                            "Exception": repr(exc),
                            "SubException Count": count,
                        },
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
                        details={
                            "Exception": repr(exc),
                            "Task": repr(task),
                        },
                    )
                    print(f"[WorkerQueue:{self._name}] Task failed: {exc!r}")
                    traceback.print_exception(type(exc), exc, exc.__traceback__)
            finally:
                if self._busy_workers > 0:
                    self._busy_workers -= 1
                self.queue.task_done()
