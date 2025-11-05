from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from .types import TaskMetadata, TaskRuntimeDetail

if TYPE_CHECKING:
    from .queue import WorkerQueue

__all__ = ["InstrumentedTask"]


class InstrumentedTask:
    __slots__ = (
        "_queue",
        "_coro",
        "_loop",
        "_enqueued_at",
        "_backlog_at_enqueue",
        "_metadata",
        "_name",
        "_closed",
    )

    def __init__(
        self,
        queue: "WorkerQueue",
        coro: Any,
        loop: asyncio.AbstractEventLoop,
        enqueued_at: float,
        backlog_at_enqueue: int,
        metadata: TaskMetadata,
    ) -> None:
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
