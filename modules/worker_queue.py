import asyncio
import itertools

class WorkerQueue:
    def __init__(self, max_workers: int = 3):
        self.queue = asyncio.PriorityQueue()
        self.max_workers = max_workers
        self.counter = itertools.count()
        self.workers = []
        self.running = False

    async def start(self):
        self.running = True
        for _ in range(self.max_workers):
            self.workers.append(asyncio.create_task(self.worker_loop()))

    async def stop(self):
        self.running = False
        for _ in range(self.max_workers):
            await self.queue.put((99, next(self.counter), None))
        await asyncio.gather(*self.workers, return_exceptions=True)

    async def add_task(self, coro, accelerated: bool = False):
        """
        Add a task to the queue.
        accelerated=True → higher priority
        """
        priority = 0 if accelerated else 1
        await self.queue.put((priority, next(self.counter), coro))

    async def worker_loop(self):
        while True:
            priority, _, task = await self.queue.get()
            if task is None:
                break
            try:
                await task
            except Exception as e:
                print(f"[WorkerQueue] Task failed: {e}")
            finally:
                self.queue.task_done()
