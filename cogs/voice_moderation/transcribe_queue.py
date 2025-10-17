import asyncio
from collections.abc import Awaitable, Callable
from typing import Generic, Optional, TypeVar

PayloadT = TypeVar("PayloadT")


class TranscriptionWorkQueue(Generic[PayloadT]):
    """Bounded work queue that limits concurrent transcription tasks.

    The queue back-pressures producers when all workers are busy and the queue
    is full, preventing unbounded harvest backlog growth.
    """

    def __init__(
        self,
        *,
        worker_count: int,
        max_queue_size: int,
        worker_fn: Callable[[PayloadT], Awaitable[None]],
    ) -> None:
        if worker_count <= 0:
            raise ValueError("worker_count must be positive")
        if max_queue_size <= 0:
            raise ValueError("max_queue_size must be positive")

        self._queue: asyncio.Queue[Optional[PayloadT]] = asyncio.Queue(max_queue_size)
        self._worker_fn = worker_fn
        self._workers: list[asyncio.Task[None]] = []
        self._worker_count = worker_count
        self._closed = False
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        self._started = True

        async def _run() -> None:
            while True:
                payload = await self._queue.get()
                try:
                    if payload is None:
                        return
                    await self._worker_fn(payload)
                finally:
                    self._queue.task_done()

        self._workers = [asyncio.create_task(_run()) for _ in range(self._worker_count)]

    async def submit(self, payload: PayloadT) -> float:
        """Enqueue work and return the time spent waiting for capacity."""
        if self._closed:
            raise RuntimeError("TranscriptionWorkQueue is closed")
        if not self._started:
            await self.start()

        start = asyncio.get_running_loop().time()
        await self._queue.put(payload)
        return asyncio.get_running_loop().time() - start

    async def drain_and_close(self) -> None:
        """Wait for all queued work, then stop all workers."""
        if self._closed:
            return
        self._closed = True
        await self._queue.join()
        for _ in self._workers:
            await self._queue.put(None)
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
