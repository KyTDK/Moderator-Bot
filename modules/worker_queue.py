import asyncio

class WorkerQueue:
    def __init__(self, max_workers: int = 3):
        self.queue = asyncio.Queue()
        self.max_workers = max_workers
        self.workers = []
        self.running = False

    async def start(self):
        self.running = True
        for _ in range(self.max_workers):
            self.workers.append(asyncio.create_task(self.worker_loop()))

    async def stop(self):
        self.running = False
        for _ in range(self.max_workers):
            await self.queue.put(None)  # Sentinel to stop
        await asyncio.gather(*self.workers, return_exceptions=True)

    async def add_task(self, coro):
        await self.queue.put(coro)

    async def resize_workers(self, new_max: int):
        if new_max == self.max_workers:
            return

        # If increasing
        if new_max > self.max_workers:
            for _ in range(new_max - self.max_workers):
                if self.running:
                    self.workers.append(asyncio.create_task(self.worker_loop()))

        # If decreasing
        elif new_max < self.max_workers:
            for _ in range(self.max_workers - new_max):
                await self.queue.put(None)  # Signal a worker to exit

        self.workers = [w for w in self.workers if not w.done()]
        self.max_workers = new_max

    async def worker_loop(self):
        while True:
            task = await self.queue.get()
            if task is None:
                break
            try:
                await task
            except Exception as e:
                print(f"[WorkerQueue] Task failed: {e}")
            finally:
                self.queue.task_done()
