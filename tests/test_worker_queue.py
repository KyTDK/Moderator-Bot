import asyncio
import builtins
import sys
from pathlib import Path

import pytest

project_root = Path(__file__).resolve().parents[1]
if str(project_root) not in sys.path:
    sys.path.append(str(project_root))

from modules.worker_queue import WorkerQueue

ExceptionGroupType = getattr(builtins, "ExceptionGroup", None)

if ExceptionGroupType is None:  # pragma: no cover - Python < 3.11 fallback
    pytest.skip("ExceptionGroup not available on this Python version", allow_module_level=True)


def test_worker_queue_handles_exception_group(capsys):
    async def runner():
        queue = WorkerQueue(max_workers=1, autoscale_max=1)
        await queue.start()

        async def failing_task():
            raise ExceptionGroupType("test group", [RuntimeError("boom")])  # type: ignore[call-arg]

        await queue.add_task(failing_task())
        await asyncio.sleep(0.05)
        await asyncio.wait_for(queue.queue.join(), timeout=1)

        captured = capsys.readouterr().out
        assert "Task group failed" in captured
        assert "RuntimeError('boom')" in captured

        await asyncio.wait_for(queue.stop(), timeout=1)

    asyncio.run(runner())


def test_worker_queue_reports_slow_singular_task():
    async def runner():
        triggered = asyncio.Event()
        reported: list[tuple[str, float]] = []

        async def reporter(detail, queue_name):
            reported.append((queue_name, detail.runtime))
            triggered.set()

        queue = WorkerQueue(
            max_workers=1,
            autoscale_max=1,
            name="singular",
            singular_task_reporter=reporter,
            singular_runtime_threshold=0.01,
        )
        await queue.start()

        async def slow_task():
            await asyncio.sleep(0.02)

        await queue.add_task(slow_task())
        await asyncio.wait_for(queue.queue.join(), timeout=1)
        await asyncio.wait_for(triggered.wait(), timeout=1)
        await asyncio.wait_for(queue.stop(), timeout=1)

        assert reported
        queue_name, runtime = reported[0]
        assert queue_name == "singular"
        assert runtime >= 0.02

    asyncio.run(runner())


def test_worker_queue_skips_non_singular_tasks_for_alerts():
    async def runner():
        reported = False

        async def reporter(*_):
            nonlocal reported
            reported = True

        queue = WorkerQueue(
            max_workers=2,
            autoscale_max=2,
            name="not_singular",
            singular_task_reporter=reporter,
            singular_runtime_threshold=0.0,
        )
        await queue.start()

        async def slow_task():
            await asyncio.sleep(0.01)

        await queue.add_task(slow_task())
        await asyncio.wait_for(queue.queue.join(), timeout=1)
        await asyncio.sleep(0.05)
        await asyncio.wait_for(queue.stop(), timeout=1)

        assert not reported

    asyncio.run(runner())


def test_worker_queue_metrics_track_busy_workers():
    async def runner():
        queue = WorkerQueue(max_workers=1, autoscale_max=1, name="busy")
        await queue.start()

        initial_metrics = queue.metrics()
        assert initial_metrics["busy_workers"] == 0

        started = asyncio.Event()
        release = asyncio.Event()

        async def work():
            started.set()
            await release.wait()

        await queue.add_task(work())
        await asyncio.wait_for(started.wait(), timeout=1)

        running_metrics = queue.metrics()
        assert running_metrics["busy_workers"] == 1

        release.set()
        await asyncio.wait_for(queue.queue.join(), timeout=1)

        final_metrics = queue.metrics()
        assert final_metrics["busy_workers"] == 0

        await asyncio.wait_for(queue.stop(), timeout=1)

    asyncio.run(runner())
