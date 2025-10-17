import asyncio
import sys
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).resolve().parents[1]))

from cogs.voice_moderation.transcribe_queue import TranscriptionWorkQueue  # noqa: E402


def run_async(coro):
    return asyncio.run(coro)


async def _test_backpressure_blocking():
    started: list[str] = []
    finish: dict[str, asyncio.Future[None]] = {
        key: asyncio.get_running_loop().create_future()
        for key in ("first", "second", "third")
    }

    async def worker(payload: tuple[str, ...]) -> None:
        key = payload[0]
        started.append(key)
        await finish[key]

    queue = TranscriptionWorkQueue(worker_count=1, max_queue_size=1, worker_fn=worker)

    await queue.submit(("first",))
    await queue.submit(("second",))
    third_task = asyncio.create_task(queue.submit(("third",)))

    await asyncio.sleep(0)
    assert not third_task.done(), "third submission should block until backlog drains"

    finish["first"].set_result(None)
    await asyncio.sleep(0)
    await asyncio.wait_for(third_task, timeout=0.1)

    # Allow remaining tasks to finish so drain_and_close can complete.
    finish["second"].set_result(None)
    finish["third"].set_result(None)
    await queue.drain_and_close()

    assert started == ["first", "second", "third"]


async def _test_worker_concurrency_limit():
    counter = 0
    peak = 0
    lock = asyncio.Lock()

    async def worker(_: int) -> None:
        nonlocal counter, peak
        async with lock:
            counter += 1
            peak = max(peak, counter)
        await asyncio.sleep(0.01)
        async with lock:
            counter -= 1

    queue = TranscriptionWorkQueue(worker_count=2, max_queue_size=4, worker_fn=worker)
    producers = [asyncio.create_task(queue.submit(i)) for i in range(8)]
    await asyncio.gather(*producers)
    await queue.drain_and_close()
    assert peak <= 2


async def _test_submit_after_close_raises():
    async def worker(_: int) -> None:
        await asyncio.sleep(0)

    queue = TranscriptionWorkQueue(worker_count=1, max_queue_size=1, worker_fn=worker)
    await queue.submit(1)
    await queue.drain_and_close()

    with pytest.raises(RuntimeError):
        await queue.submit(2)


def test_transcription_queue_backpressure_blocks():
    run_async(_test_backpressure_blocking())


def test_transcription_queue_limits_concurrency():
    run_async(_test_worker_concurrency_limit())


def test_transcription_queue_submit_after_close():
    run_async(_test_submit_after_close_raises())
