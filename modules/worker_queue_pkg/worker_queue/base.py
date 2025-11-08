from __future__ import annotations

import asyncio
import logging
from typing import Optional

import discord

from ..notifier import QueueEventNotifier
from ..types import SlowTaskReporter
from .events import QueueEventLogger
from .mixins.autoscale import AutoscaleMixin
from .mixins.backlog import BacklogMixin
from .mixins.instrumentation_support import InstrumentationSupportMixin
from .mixins.lifecycle import LifecycleMixin
from .mixins.metrics import MetricsMixin
from .rate_tracker import RateTracker

__all__ = ["WorkerQueue"]


class WorkerQueue(
    LifecycleMixin,
    AutoscaleMixin,
    BacklogMixin,
    MetricsMixin,
    InstrumentationSupportMixin,
):
    def __init__(
        self,
        max_workers: int = 3,
        *,
        autoscale_max: Optional[int] = None,
        backlog_high_watermark: int = 30,
        backlog_low_watermark: int = 5,
        autoscale_check_interval: float = 2.0,
        scale_down_grace: float = 5.0,
        name: Optional[str] = None,
        backlog_hard_limit: Optional[int] = 500,
        backlog_shed_to: Optional[int] = None,
        singular_task_reporter: Optional[SlowTaskReporter] = None,
        singular_runtime_threshold: Optional[float] = None,
        developer_log_bot: Optional[discord.Client] = None,
        developer_log_context: Optional[str] = None,
        adaptive_mode: bool = False,
        rate_tracking_window: float = 180.0,
    ):
        self.queue = asyncio.Queue()
        self.max_workers = max_workers
        self._baseline_workers = max_workers
        self._autoscale_max = autoscale_max or max_workers

        self._backlog_high = backlog_high_watermark
        self._backlog_low = backlog_low_watermark
        self._check_interval = autoscale_check_interval
        self._scale_down_grace = scale_down_grace

        self._backlog_hard_limit = backlog_hard_limit
        self._backlog_shed_to = backlog_shed_to

        self._name = name or "queue"

        self.workers: list[asyncio.Task] = []
        self._busy_workers: int = 0
        self.running = False
        self._lock = asyncio.Lock()
        self._autoscaler_task: Optional[asyncio.Task] = None
        self._pending_stops: int = 0

        self._log = logging.getLogger(f"{__name__}.{self._name}")
        self._notifier = QueueEventNotifier(
            queue_name=self._name,
            logger=self._log,
            developer_bot=developer_log_bot,
            developer_context=developer_log_context,
        )
        self._events = QueueEventLogger(name=self._name, notifier=self._notifier)

        self._setup_instrumentation(
            singular_task_reporter=singular_task_reporter,
            singular_runtime_threshold=singular_runtime_threshold,
        )

        self._configured_autoscale_max: int = self._autoscale_max
        self._adaptive_step: int = 1
        self._adaptive_backlog_hits: int = 0
        self._adaptive_recovery_hits: int = 0
        self._adaptive_hit_threshold: int = 4
        self._adaptive_reset_hits: int = 12
        self._adaptive_bump_cooldown: float = 30.0
        self._last_adaptive_bump: float = 0.0
        self._adaptive_ceiling: int = 0

        self._adaptive_mode: bool = bool(adaptive_mode)
        self._rate_window: float = max(30.0, float(rate_tracking_window))
        self._arrival_tracker = RateTracker(window=self._rate_window)
        self._completion_tracker = RateTracker(window=self._rate_window)
        self._adaptive_plan_target: int = self.max_workers
        self._adaptive_plan_baseline: int = self._baseline_workers
        self._last_plan_applied: float = 0.0

        self._recompute_adaptive_ceiling()

    # Instrumentation mixin hooks ensure `_record_wait`, `_record_runtime`,
    # and `_handle_task_complete` delegate into the instrumentation layer.
