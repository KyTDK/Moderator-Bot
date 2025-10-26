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
