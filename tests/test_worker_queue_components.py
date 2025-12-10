import asyncio
from typing import Optional

import pytest

from modules.worker_queue_pkg.worker_queue.events import QueueEventLogger
from modules.worker_queue_pkg.worker_queue.instrumentation import QueueInstrumentation
from modules.worker_queue_pkg.worker_queue.rate_tracker import RateTracker
from modules.worker_queue_pkg.types import TaskMetadata, TaskRuntimeDetail


class FakeNotifier:
    def __init__(self) -> None:
        self.records: list[tuple[str, str, Optional[str], Optional[dict[str, object]]]] = []

    def info(
        self,
        message: str,
        *,
        event_key: str | None = None,
        details: Optional[dict[str, object]] = None,
    ) -> None:
        self.records.append(("info", message, event_key, details))

    def warning(
        self,
        message: str,
        *,
        event_key: str | None = None,
        details: Optional[dict[str, object]] = None,
    ) -> None:
        self.records.append(("warning", message, event_key, details))

    def error(
        self,
        message: str,
        *,
        event_key: str | None = None,
        details: Optional[dict[str, object]] = None,
    ) -> None:
        self.records.append(("error", message, event_key, details))

    def debug(
        self,
        message: str,
        *,
        event_key: str | None = None,
        details: Optional[dict[str, object]] = None,
    ) -> None:
        self.records.append(("debug", message, event_key, details))


def _runtime_detail(*, runtime: float, max_workers: int = 1, autoscale_max: int = 1) -> TaskRuntimeDetail:
    metadata = TaskMetadata(
        display_name="task",
        module="tests",
        qualname="tests.task",
        function="task",
        filename="tests/test_worker_queue_components.py",
        first_lineno=0,
    )
    return TaskRuntimeDetail(
        metadata=metadata,
        wait=0.0,
        runtime=runtime,
        enqueued_at_monotonic=0.0,
        started_at_monotonic=0.0,
        completed_at_monotonic=runtime,
        started_at_wall=0.0,
        completed_at_wall=runtime,
        backlog_at_enqueue=0,
        backlog_at_start=0,
        backlog_at_finish=0,
        active_workers_start=max_workers,
        busy_workers_start=max_workers,
        max_workers=max_workers,
        autoscale_max=autoscale_max,
    )


def test_rate_tracker_basic(monkeypatch: pytest.MonkeyPatch) -> None:
    current_time = 0.0

    def fake_monotonic() -> float:
        return current_time

    monkeypatch.setattr("modules.worker_queue_pkg.worker_queue.rate_tracker.time.monotonic", fake_monotonic)

    tracker = RateTracker(window=60.0)

    current_time = 0.0
    tracker.record()

    current_time = 30.0
    tracker.record()

    current_time = 60.0
    assert tracker.rate_per_minute() == pytest.approx(2.0)

    current_time = 200.0
    assert tracker.rate_per_minute() == pytest.approx(0.0)


def test_instrumentation_records_metrics_and_reports_singular() -> None:
    async def runner() -> None:
        reporter_calls: list[str] = []

        async def reporter(detail: TaskRuntimeDetail, queue_name: str) -> None:
            reporter_calls.append(queue_name)

        notifier = FakeNotifier()
        instrumentation = QueueInstrumentation(
            queue_name="test",
            notifier=notifier,  # type: ignore[arg-type]
            singular_task_reporter=reporter,
            singular_runtime_threshold=0.01,
            slow_wait_threshold=5.0,
        )

        instrumentation.record_wait(2.0)
        detail = _runtime_detail(runtime=0.5)
        instrumentation.record_runtime(detail)
        await instrumentation.wait_for_alerts()

        metrics = instrumentation.metrics_payload()
        assert metrics["tasks_completed"] == 1
        assert metrics["avg_runtime"] == pytest.approx(0.5)
        assert metrics["avg_wait_time"] == pytest.approx(2.0)
        assert reporter_calls == ["test"]

        # Non-singular task should not trigger additional reports.
        detail_non_singular = _runtime_detail(runtime=0.2, max_workers=2, autoscale_max=2)
        instrumentation.record_runtime(detail_non_singular)
        await instrumentation.wait_for_alerts()
        assert reporter_calls == ["test"]

    asyncio.run(runner())


def test_queue_event_logger_emits_expected_messages() -> None:
    notifier = FakeNotifier()
    logger = QueueEventLogger(name="unit", notifier=notifier)  # type: ignore[arg-type]

    logger.scaled_up(old=1, new=3, reason="test")
    logger.scaled_down(old=3, new=1, reason="cleanup")
    logger.adaptive_plan_updated(changes=["target 1->2"], target=2, baseline=1, backlog_high=10)

    severity, message, event_key, details = notifier.records[0]
    assert severity == "info"
    assert message == "[WorkerQueue:unit] scaled up 1->3 (reason=test)"
    assert event_key == "scale_up:3"
    assert details is None

    severity, message, event_key, details = notifier.records[1]
    assert severity == "info"
    assert message == "[WorkerQueue:unit] scaled down 3->1 (reason=cleanup)"
    assert event_key == "scale_down:1"
    assert details is None

    severity, message, event_key, details = notifier.records[2]
    assert severity == "debug"
    assert event_key == "adaptive_plan:2:1:10"
    assert "adaptive plan updated: target 1->2" in message
