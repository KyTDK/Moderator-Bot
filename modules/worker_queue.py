import asyncio
import builtins
import time
import traceback
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional


@dataclass(slots=True)
class TaskMetadata:
    display_name: str
    module: Optional[str]
    qualname: Optional[str]
    function: Optional[str]
    filename: Optional[str]
    first_lineno: Optional[int]

    @classmethod
    def from_coroutine(cls, coro) -> "TaskMetadata":
        """Extract identifying information for a coroutine."""
        code = getattr(coro, "cr_code", None)
        qualname = getattr(code, "co_qualname", None) if code is not None else None
        func_name = getattr(code, "co_name", None) if code is not None else None
        filename = getattr(code, "co_filename", None) if code is not None else None
        first_lineno = getattr(code, "co_firstlineno", None) if code is not None else None

        module = getattr(coro, "__module__", None)
        if module is None:
            frame = getattr(coro, "cr_frame", None)
            if frame is not None:
                module = frame.f_globals.get("__name__")

        fallback = getattr(coro, "__qualname__", None) or getattr(coro, "__name__", None)
        name = qualname or func_name or fallback
        if not name and module:
            name = f"{module}.<coroutine>"
        display_name = str(name) if name else repr(coro)

        return cls(
            display_name=display_name,
            module=module,
            qualname=qualname,
            function=func_name,
            filename=filename,
            first_lineno=first_lineno,
        )


@dataclass(slots=True)
class TaskRuntimeDetail:
    metadata: TaskMetadata
    wait: float
    runtime: float
    enqueued_at_monotonic: float
    started_at_monotonic: float
    completed_at_monotonic: float
    started_at_wall: float
    completed_at_wall: float
    backlog_at_enqueue: int
    backlog_at_start: int
    backlog_at_finish: int
    active_workers_start: int
    busy_workers_start: int
    max_workers: int
    autoscale_max: int

    def as_mapping(self) -> dict[str, Any]:
        return {
            "display_name": self.metadata.display_name,
            "module": self.metadata.module,
            "qualname": self.metadata.qualname,
            "function": self.metadata.function,
            "filename": self.metadata.filename,
            "first_lineno": self.metadata.first_lineno,
            "wait": self.wait,
            "runtime": self.runtime,
            "enqueued_at_monotonic": self.enqueued_at_monotonic,
            "started_at_monotonic": self.started_at_monotonic,
            "completed_at_monotonic": self.completed_at_monotonic,
            "started_at_wall": self.started_at_wall,
            "completed_at_wall": self.completed_at_wall,
            "backlog_at_enqueue": self.backlog_at_enqueue,
            "backlog_at_start": self.backlog_at_start,
            "backlog_at_finish": self.backlog_at_finish,
            "active_workers_start": self.active_workers_start,
            "busy_workers_start": self.busy_workers_start,
            "max_workers": self.max_workers,
            "autoscale_max": self.autoscale_max,
        }


SlowTaskReporter = Callable[["TaskRuntimeDetail", str], Awaitable[None]]


class _InstrumentedTask:
    __slots__ = (
        "_queue",
        "_coro",
        "_loop",
        "_enqueued_at",
        "_backlog_at_enqueue",
        "_name",
        "_metadata",
        "_closed",
    )

    def __init__(self, queue, coro, loop, enqueued_at, backlog_at_enqueue, metadata: TaskMetadata) -> None:
        self._queue = queue
        self._coro = coro
        self._loop = loop
        self._enqueued_at = enqueued_at
        self._backlog_at_enqueue = backlog_at_enqueue
        self._metadata = metadata
        self._name = metadata.display_name
        self._closed = False

    def __await__(self):
        return self._run().__await__()

    async def _run(self):
        started_at = self._loop.time()
        started_wall = time.time()
        wait_duration = started_at - self._enqueued_at
        queue = self._queue
        queue._record_wait(wait_duration)
        if wait_duration > queue._slow_wait_threshold:
            queue._maybe_log_wait(wait_duration, self._backlog_at_enqueue, self._name)

        backlog_at_start = queue.queue.qsize()
        active_workers_start = queue._active_workers()
        busy_workers_start = queue._busy_workers

        try:
            await self._coro
        finally:
            completed_at = self._loop.time()
            completed_wall = time.time()
            runtime = completed_at - started_at
            backlog_at_finish = queue.queue.qsize()
            detail = TaskRuntimeDetail(
                metadata=self._metadata,
                wait=wait_duration,
                runtime=runtime,
                enqueued_at_monotonic=self._enqueued_at,
                started_at_monotonic=started_at,
                completed_at_monotonic=completed_at,
                started_at_wall=started_wall,
                completed_at_wall=completed_wall,
                backlog_at_enqueue=self._backlog_at_enqueue,
                backlog_at_start=backlog_at_start,
                backlog_at_finish=backlog_at_finish,
                active_workers_start=active_workers_start,
                busy_workers_start=busy_workers_start,
                max_workers=queue.max_workers,
                autoscale_max=queue._autoscale_max,
            )
            queue._handle_task_complete(detail, runtime, self._name)
            self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._queue._close_coroutine(self._coro)

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
        self._busy_workers: int = 0
        self.running = False
        self._lock = asyncio.Lock()
        self._autoscaler_task: Optional[asyncio.Task] = None
        self._pending_stops: int = 0  # queued stop signals not yet consumed

        # Metrics
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
        if singular_runtime_threshold is None:
            singular_runtime_threshold = float(
                getattr(singular_task_reporter, "threshold", 30.0)
            )
        self._singular_runtime_threshold: float = float(singular_runtime_threshold)
        self._singular_task_reporter = singular_task_reporter
        self._alert_tasks: set[asyncio.Task[Any]] = set()

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
            if self._alert_tasks:
                await asyncio.gather(*self._alert_tasks, return_exceptions=True)
                self._alert_tasks.clear()

    async def add_task(self, coro):
        if not asyncio.iscoroutine(coro):
            raise TypeError("add_task expects a coroutine")
        await self.queue.put(self._wrap_task(coro))
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

    async def ensure_capacity(self, target_workers: int):
        """Ensure the queue can scale up to the requested worker count."""
        target_workers = max(1, int(target_workers))
        needs_resize = False

        async with self._lock:
            if target_workers > self._autoscale_max:
                self._autoscale_max = target_workers
            if target_workers > self.max_workers:
                needs_resize = True

        if needs_resize:
            await self.resize_workers(target_workers)

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
                self._close_enqueued_coroutine(item)
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

    def _wrap_task(self, coro):
        loop = asyncio.get_running_loop()
        enqueued_at = loop.time()
        metadata = self._task_metadata(coro)
        backlog_at_enqueue = self.queue.qsize()

        return _InstrumentedTask(self, coro, loop, enqueued_at, backlog_at_enqueue, metadata)

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
        now = time.monotonic()
        if now - self._last_wait_log < self._slow_log_cooldown:
            return
        self._last_wait_log = now
        print(
            f"[WorkerQueue:{self._name}] Task {name!r} waited {wait:.2f}s before starting "
            f"(backlog_at_enqueue={backlog}, current_backlog={self.queue.qsize()}, workers={self._active_workers()}/{self.max_workers})"
        )

    def _maybe_log_runtime(self, runtime: float, name: str) -> None:
        now = time.monotonic()
        if now - self._last_runtime_log < self._slow_log_cooldown:
            return
        self._last_runtime_log = now
        print(
            f"[WorkerQueue:{self._name}] Task {name!r} ran for {runtime:.2f}s "
            f"(current_backlog={self.queue.qsize()}, workers={self._active_workers()}/{self.max_workers})"
        )

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
        self, reporter: SlowTaskReporter, detail: TaskRuntimeDetail
    ) -> None:
        try:
            task = asyncio.create_task(reporter(detail, self._name))
        except RuntimeError:
            print(
                f"[WorkerQueue:{self._name}] Unable to schedule singular task alert; no running event loop."
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
            print(
                f"[WorkerQueue:{self._name}] Singular task reporter failed: {exc!r}"
            )

    def _close_enqueued_coroutine(self, item) -> None:
        """Best-effort close of instrumented wrapper."""
        if isinstance(item, _InstrumentedTask):
            item.close()
            return

        close = getattr(item, "close", None)
        if callable(close):
            try:
                close()
            except RuntimeError:
                # Coroutine might currently be running; ignore.
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
                if self._busy_workers > 0:
                    self._busy_workers -= 1
                self.queue.task_done()

    def metrics(self) -> dict:
        """Return a snapshot of queue and autoscaler metrics."""
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
