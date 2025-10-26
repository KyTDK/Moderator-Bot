import asyncio
import builtins
import traceback
from typing import Optional

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
    ):
        self.queue = asyncio.Queue()
        # Current configured max (may change via autoscaler/resize)
        self.max_workers = max_workers
        # Baseline max to return to when backlog clears
        self._baseline_workers = max_workers
        # Upper bound during autoscale bursts
        self._autoscale_max = autoscale_max or max_workers

        # Autoscaler tuning
        self._backlog_high = backlog_high_watermark
        self._backlog_low = backlog_low_watermark
        self._check_interval = autoscale_check_interval
        self._scale_down_grace = scale_down_grace

        # Backlog shedding
        self._backlog_hard_limit = backlog_hard_limit
        self._backlog_shed_to = backlog_shed_to

        self._name = name or "queue"

        self.workers: list[asyncio.Task] = []
        self.running = False
        self._lock = asyncio.Lock()
        self._autoscaler_task: Optional[asyncio.Task] = None
        self._pending_stops: int = 0  # queued stop signals not yet consumed

        # Metrics
        self._metrics_dropped: int = 0

    def _active_workers(self) -> int:
        """Count non-finished worker tasks."""
        return sum(1 for w in self.workers if not w.done())

    async def start(self):
        async with self._lock:
            if self.running:
                return
            self.running = True
            for _ in range(self.max_workers):
                self.workers.append(asyncio.create_task(self.worker_loop()))
            # Start autoscaler if burst is possible
            if self._autoscale_max > self._baseline_workers:
                self._autoscaler_task = asyncio.create_task(self.autoscaler_loop())

    async def stop(self):
        async with self._lock:
            if not self.running:
                return
            self.running = False
            # Stop autoscaler first
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

    async def add_task(self, coro):
        if not asyncio.iscoroutine(coro):
            raise TypeError("add_task expects a coroutine")
        await self.queue.put(coro)
        # If backlog shedding is enabled and we're over the hard limit, shed immediately.
        await self._shed_backlog_if_needed(trigger="put")

    async def resize_workers(self, new_max: int):
        async with self._lock:
            if new_max == self.max_workers:
                return

            # scale up
            if new_max > self.max_workers:
                self.max_workers = new_max
                if self.running:
                    # Start only as many as needed based on active count
                    active = self._active_workers()
                    need = new_max - active
                    for _ in range(max(0, need)):
                        self.workers.append(asyncio.create_task(self.worker_loop()))
                return

            # scale down
            active = self._active_workers()
            deficit = max(0, active - new_max)
            # Only queue additional stop tokens to reach the target
            to_stop = max(0, deficit - self._pending_stops)
            for _ in range(to_stop):
                await self.queue.put(_SENTINEL)
            self._pending_stops += to_stop
            self.max_workers = new_max
            # prune
            self.workers = [w for w in self.workers if not w.done()]

    async def autoscaler_loop(self):
        """Periodically checks backlog and adjusts worker count.

        - If backlog >= high watermark, scale to autoscale_max immediately.
        - When backlog <= low watermark for a grace period, scale back to baseline.
        - If backlog exceeds hard limit (when configured), shed the oldest tasks.
        """
        low_since: Optional[float] = None
        loop = asyncio.get_running_loop()
        try:
            while self.running:
                await asyncio.sleep(self._check_interval)
                # Snapshot current backlog
                q = self.queue.qsize()
                # Periodically prune finished workers to keep counts accurate
                self.workers = [w for w in self.workers if not w.done()]
                active = self._active_workers()

                # Backlog hard limit: shed before anything else
                await self._shed_backlog_if_needed(trigger="autoscaler")

                # Upscale when backlog is significant
                if q >= self._backlog_high and active < self._autoscale_max:
                    await self.resize_workers(self._autoscale_max)
                    low_since = None
                    print(f"[WorkerQueue:{self._name}] Backlog {q} >= {self._backlog_high}, scaling up to {self._autoscale_max}")
                    continue

                # Consider downscaling if low backlog and currently above baseline
                over_baseline = max(0, active - self._baseline_workers - self._pending_stops)
                if q <= self._backlog_low and over_baseline > 0:
                    now = loop.time()
                    if low_since is None:
                        low_since = now
                    elif (now - low_since) >= self._scale_down_grace:
                        await self.resize_workers(self._baseline_workers)
                        print(f"[WorkerQueue:{self._name}] Backlog stable (<= {self._backlog_low}), scaling down to baseline {self._baseline_workers}")
                        low_since = None
                else:
                    # Reset grace timer if backlog rose again
                    low_since = None
        except asyncio.CancelledError:
            pass

    async def _shed_backlog_if_needed(self, *, trigger: str) -> int:
        """If a hard backlog limit is configured and exceeded, drop oldest tasks.

        Returns number of tasks dropped.
        """
        if self._backlog_hard_limit is None:
            return 0

        q = self.queue.qsize()
        if q <= self._backlog_hard_limit:
            return 0

        # Determine target size to shed down to
        target = self._backlog_shed_to
        if target is None:
            target = self._backlog_high  # default to high watermark

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
                    # Requeue sentinel at the end; do not count as dropped
                    returned_sentinels += 1
                    # Mark this removal as handled for queue accounting
                    self.queue.task_done()
                    continue
                # Drop the task (do not execute)
                try:
                    # Best-effort: if coroutine supports .close(), close it to free resources
                    if hasattr(item, "close"):
                        try:
                            item.close()  # type: ignore[attr-defined]
                        except Exception:
                            pass
                except Exception:
                    pass
                dropped += 1
                # Mark this task as done since we're discarding it
                self.queue.task_done()

            # Re-enqueue any sentinels we pulled off
            for _ in range(returned_sentinels):
                await self.queue.put(_SENTINEL)

        self._metrics_dropped += dropped
        if dropped:
            print(f"[WorkerQueue:{self._name}] Backlog {q} exceeded hard limit {self._backlog_hard_limit}; dropped {dropped} oldest task(s) (trigger={trigger}).")
        return dropped

    async def worker_loop(self):
        while True:
            task = await self.queue.get()
            if task is _SENTINEL:
                self.queue.task_done()
                if self._pending_stops > 0:
                    self._pending_stops -= 1
                break
            try:
                await task
            except asyncio.CancelledError:
                print(f"[WorkerQueue:{self._name}] Task cancelled.")
            except BaseException as exc:  # noqa: BLE001
                base_group = getattr(builtins, "BaseExceptionGroup", None)
                if base_group is not None and isinstance(exc, base_group):
                    sub_exceptions = exc.exceptions  # type: ignore[attr-defined]
                    count = len(sub_exceptions)
                    print(f"[WorkerQueue:{self._name}] Task group failed with {count} sub-exception(s).")
                    for idx, sub_exc in enumerate(sub_exceptions, start=1):
                        print(f"[WorkerQueue:{self._name}] ├─ Sub-exception {idx}/{count}: {sub_exc!r}")
                        traceback.print_exception(type(sub_exc), sub_exc, sub_exc.__traceback__)
                else:
                    print(f"[WorkerQueue:{self._name}] Task failed: {exc!r}")
                    traceback.print_exception(type(exc), exc, exc.__traceback__)
            finally:
                self.queue.task_done()

    def metrics(self) -> dict:
        """Return a snapshot of queue and autoscaler metrics."""
        return {
            "name": self._name,
            "running": self.running,
            "backlog": self.queue.qsize(),
            "active_workers": self._active_workers(),
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
        }
