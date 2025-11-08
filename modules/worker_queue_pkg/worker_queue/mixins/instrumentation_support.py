from __future__ import annotations

from typing import Optional

from ...types import SlowTaskReporter, TaskRuntimeDetail
from ..instrumentation import QueueInstrumentation

__all__ = ["InstrumentationSupportMixin"]


class InstrumentationSupportMixin:
    """Centralised instrumentation plumbing for worker queues."""

    def _setup_instrumentation(
        self,
        *,
        singular_task_reporter: Optional[SlowTaskReporter],
        singular_runtime_threshold: Optional[float],
    ) -> None:
        if singular_runtime_threshold is None:
            singular_runtime_threshold = float(
                getattr(singular_task_reporter, "threshold", 30.0)
            )
        instrumentation_threshold = float(singular_runtime_threshold)

        self._singular_task_reporter = singular_task_reporter
        self._instrumentation = QueueInstrumentation(
            queue_name=self._name,
            notifier=self._notifier,
            singular_task_reporter=singular_task_reporter,
            singular_runtime_threshold=instrumentation_threshold,
            slow_wait_threshold=15.0,
        )

    # ------------------------------------------------------------------ #
    # Instrumentation delegates
    # ------------------------------------------------------------------ #

    def _record_wait(self, wait: float) -> None:
        self._instrumentation.record_wait(wait)

    def _record_runtime(self, detail: TaskRuntimeDetail) -> None:
        self._instrumentation.record_runtime(detail)

    def _handle_task_complete(self, detail: TaskRuntimeDetail, runtime: float, name: str) -> None:
        self._record_runtime(detail)
        self._record_completion()
