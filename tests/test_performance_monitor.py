from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# Stub out heavy optional dependencies pulled in by AggregatedModerationCog imports.
nsfw_stub = types.ModuleType("modules.nsfw_scanner")
nsfw_stub.NSFWScanner = object
nsfw_stub.handle_nsfw_content = lambda *_, **__: None
nsfw_stub.__path__ = []  # Mark as a package for sibling imports.
sys.modules.setdefault("modules.nsfw_scanner", nsfw_stub)

actions_stub = types.ModuleType("modules.nsfw_scanner.actions")
actions_stub.handle_nsfw_content = lambda *_, **__: None
sys.modules.setdefault("modules.nsfw_scanner.actions", actions_stub)

constants_stub = types.ModuleType("modules.nsfw_scanner.constants")
constants_stub.NSFW_STATUS = types.SimpleNamespace()
constants_stub.LOG_CHANNEL_ID = None
sys.modules.setdefault("modules.nsfw_scanner.constants", constants_stub)

settings_stub = types.ModuleType("modules.nsfw_scanner.settings_keys")
settings_stub.NSFW_TEXT_ENABLED_SETTING = ""
settings_stub.NSFW_TEXT_EXCLUDED_CHANNELS_SETTING = ""
sys.modules.setdefault("modules.nsfw_scanner.settings_keys", settings_stub)

helpers_stub = types.ModuleType("modules.nsfw_scanner.helpers")
helpers_stub.AttachmentSettingsCache = object
helpers_stub.check_attachment = lambda *_, **__: None
helpers_stub.determine_frames_to_analyze = lambda *_, **__: []
helpers_stub.get_viewer_mode_from_message = lambda *_, **__: None
sys.modules.setdefault("modules.nsfw_scanner.helpers", helpers_stub)


class _FakeQueue:
    def __init__(self, metrics: dict):
        self._metrics = metrics
        self.running = True

    def metrics(self):
        return self._metrics


def _queue_metrics(name: str, *, tasks_completed: int, avg_runtime: float) -> dict:
    return {
        "name": name,
        "backlog": 0,
        "active_workers": 1,
        "busy_workers": 1,
        "max_workers": 1,
        "baseline_workers": 1,
        "autoscale_max": 1,
        "pending_stops": 0,
        "backlog_high": None,
        "backlog_low": None,
        "backlog_hard_limit": None,
        "backlog_shed_to": None,
        "dropped_tasks_total": 0,
        "tasks_completed": tasks_completed,
        "avg_runtime": avg_runtime,
        "avg_wait_time": 0.0,
        "ema_runtime": avg_runtime,
        "ema_wait_time": 0.0,
        "last_runtime": avg_runtime,
        "last_wait_time": 0.0,
        "longest_runtime": avg_runtime,
        "longest_wait": 0.0,
        "last_runtime_details": {},
        "longest_runtime_details": {},
        "check_interval": 1.0,
        "scale_down_grace": 1.0,
    }


@pytest.fixture(scope="module")
def anyio_backend():
    return "asyncio"


def test_accelerated_performance_monitor_ignores_video_queue():
    from cogs.aggregated_moderation.config import load_config
    from cogs.aggregated_moderation.performance_monitor import (
        AcceleratedPerformanceMonitor,
    )

    free_queue = _FakeQueue(_queue_metrics("free", tasks_completed=10, avg_runtime=0.5))

    accelerated_queue = _FakeQueue(
        _queue_metrics("accelerated", tasks_completed=20, avg_runtime=0.6)
    )
    accelerated_text_queue = _FakeQueue(
        _queue_metrics("accelerated_text", tasks_completed=5, avg_runtime=0.4)
    )

    # The video queue is intentionally heavy; if included it would dominate averages.
    accelerated_video_queue = _FakeQueue(
        _queue_metrics("accelerated_video", tasks_completed=50, avg_runtime=3.5)
    )

    monitor = AcceleratedPerformanceMonitor(
        bot=types.SimpleNamespace(),
        free_queue=free_queue,
        accelerated_queue=accelerated_queue,
        accelerated_text_queue=accelerated_text_queue,
        video_queue=accelerated_video_queue,
        config=load_config(),
    )

    snapshot = monitor._build_accelerated_snapshot()

    # Only the main accelerated queues should be represented.
    assert snapshot.tasks_completed == 25
    assert snapshot.avg_runtime == pytest.approx(0.56)
