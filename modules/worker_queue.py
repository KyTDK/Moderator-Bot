import asyncio

_SENTINEL = object()

class WorkerQueue:
    def __init__(self, max_workers: int = 3):
        self.queue = asyncio.Queue()
        self.max_workers = max_workers
        self.workers: list[asyncio.Task] = []
        self.running = False
        self._lock = asyncio.Lock()

    async def start(self):
        async with self._lock:
            if self.running:
                return
            self.running = True
            for _ in range(self.max_workers):
                self.workers.append(asyncio.create_task(self.worker_loop()))

    async def stop(self):
        async with self._lock:
            if not self.running:
                return
            self.running = False
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