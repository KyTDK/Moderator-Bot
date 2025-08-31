import asyncio
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
        scale_down_grace: float = 15.0,
        name: Optional[str] = None,
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

        self._name = name or "queue"

        self.workers: list[asyncio.Task] = []
        self.running = False
        self._lock = asyncio.Lock()
        self._autoscaler_task: Optional[asyncio.Task] = None

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

    async def add_task(self, coro):
        if not asyncio.iscoroutine(coro):
            raise TypeError("add_task expects a coroutine")
        await self.queue.put(coro)

    async def resize_workers(self, new_max: int):
        async with self._lock:
            if new_max == self.max_workers:
                return

            # scale up
            if new_max > self.max_workers:
                self.max_workers = new_max
                if self.running:
                    need = new_max - len(self.workers)
                    for _ in range(max(0, need)):
                        self.workers.append(asyncio.create_task(self.worker_loop()))
                return

            # scale down
            to_stop = max(0, len(self.workers) - new_max)
            for _ in range(to_stop):
                await self.queue.put(_SENTINEL)
            self.max_workers = new_max
            # prune
            self.workers = [w for w in self.workers if not w.done()]

    async def autoscaler_loop(self):
        """Periodically checks backlog and adjusts worker count.

        - If backlog >= high watermark, scale to autoscale_max immediately.
        - When backlog <= low watermark for a grace period, scale back to baseline.
        """
        low_since: Optional[float] = None
        loop = asyncio.get_running_loop()
        try:
            while self.running:
                await asyncio.sleep(self._check_interval)
                # Snapshot current backlog
                q = self.queue.qsize()

                # Upscale when backlog is significant
                if q >= self._backlog_high and len(self.workers) < self._autoscale_max:
                    # Jump to burst max to clear the backlog
                    await self.resize_workers(self._autoscale_max)
                    low_since = None
                    print(f"[WorkerQueue:{self._name}] Backlog {q} >= {self._backlog_high}, scaling up to {self._autoscale_max}")
                    continue

                # Consider downscaling if low backlog and currently above baseline
                if q <= self._backlog_low and len(self.workers) > self._baseline_workers:
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

    async def worker_loop(self):
        while True:
            task = await self.queue.get()
            if task is _SENTINEL:
                self.queue.task_done()
                break
            try:
                await task
            except Exception as e:
                print(f"[WorkerQueue] Task failed: {e}")
            finally:
                self.queue.task_done()

    def metrics(self) -> dict:
        """Return a snapshot of queue and autoscaler metrics."""
        return {
            "name": self._name,
            "running": self.running,
            "backlog": self.queue.qsize(),
            "active_workers": len(self.workers),
            "max_workers": self.max_workers,
            "baseline_workers": self._baseline_workers,
            "autoscale_max": self._autoscale_max,
            "backlog_high": self._backlog_high,
            "backlog_low": self._backlog_low,
            "check_interval": self._check_interval,
            "scale_down_grace": self._scale_down_grace,
        }
