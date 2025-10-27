from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional


def _int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass(slots=True)
class QueueSnapshot:
    """Immutable view of a worker queue's metrics."""

    name: str
    backlog: int
    active_workers: int
    max_workers: int
    baseline_workers: int
    autoscale_max: int
    pending_stops: int
    backlog_high: Optional[int]
    backlog_low: Optional[int]
    backlog_hard_limit: Optional[int]
    backlog_shed_to: Optional[int]
    dropped_total: int
    tasks_completed: int
    avg_runtime: float
    avg_wait: float
    ema_runtime: float
    ema_wait: float
    last_runtime: float
    last_wait: float
    longest_runtime: float
    longest_wait: float
    check_interval: float
    scale_down_grace: float

    @classmethod
    def from_mapping(cls, metrics: Mapping[str, Any]) -> QueueSnapshot:
        baseline = max(1, _int(metrics.get("baseline_workers"), 1))
        return cls(
            name=str(metrics.get("name") or "queue"),
            backlog=_int(metrics.get("backlog")),
            active_workers=_int(metrics.get("active_workers")),
            max_workers=_int(metrics.get("max_workers"), 1),
            baseline_workers=baseline,
            autoscale_max=_int(metrics.get("autoscale_max")),
            pending_stops=_int(metrics.get("pending_stops")),
            backlog_high=_int(metrics.get("backlog_high")) if metrics.get("backlog_high") is not None else None,
            backlog_low=_int(metrics.get("backlog_low")) if metrics.get("backlog_low") is not None else None,
            backlog_hard_limit=_int(metrics.get("backlog_hard_limit")) if metrics.get("backlog_hard_limit") is not None else None,
            backlog_shed_to=_int(metrics.get("backlog_shed_to")) if metrics.get("backlog_shed_to") is not None else None,
            dropped_total=_int(metrics.get("dropped_tasks_total")),
            tasks_completed=_int(metrics.get("tasks_completed")),
            avg_runtime=_float(metrics.get("avg_runtime")),
            avg_wait=_float(metrics.get("avg_wait_time")),
            ema_runtime=_float(metrics.get("ema_runtime")),
            ema_wait=_float(metrics.get("ema_wait_time")),
            last_runtime=_float(metrics.get("last_runtime")),
            last_wait=_float(metrics.get("last_wait_time")),
            longest_runtime=_float(metrics.get("longest_runtime")),
            longest_wait=_float(metrics.get("longest_wait")),
            check_interval=_float(metrics.get("check_interval")),
            scale_down_grace=_float(metrics.get("scale_down_grace")),
        )

    @property
    def capacity(self) -> int:
        """Current usable worker capacity."""
        return max(self.max_workers, self.baseline_workers)

    @property
    def backlog_ratio(self) -> float:
        if not self.backlog_high:
            return 0.0
        if self.backlog_high <= 0:
            return 0.0
        return self.backlog / self.backlog_high

    @property
    def backlog_excess(self) -> int:
        if self.backlog_high and self.backlog_high > 0:
            return max(0, self.backlog - self.backlog_high)
        return max(0, self.backlog - self.capacity)

    @property
    def wait_pressure(self) -> bool:
        if self.avg_runtime > 0.0:
            if self.avg_wait >= max(5.0, self.avg_runtime * 2.0):
                return True
            if self.last_wait >= max(10.0, self.avg_runtime * 2.5):
                return True
            if self.longest_wait >= max(15.0, self.avg_runtime * 3.0):
                return True
            return False
        return max(self.avg_wait, self.last_wait, self.longest_wait) >= 10.0

    def format_lines(self) -> str:
        parts = [
            f"Backlog: {self.backlog}",
            f"Workers: {self.active_workers}/{self.max_workers} (baseline {self.baseline_workers}, burst {self.autoscale_max})",
            f"Pending stops: {self.pending_stops}",
            f"Watermarks: high={self.backlog_high}, low={self.backlog_low}",
        ]
        if self.backlog_hard_limit is not None:
            parts.append(f"Hard limit: {self.backlog_hard_limit} -> shed to {self.backlog_shed_to}")
        parts.append(f"Dropped total: {self.dropped_total}")
        parts.append(
            "Task timings: "
            f"avg_run={self.avg_runtime:.2f}s (ema {self.ema_runtime:.2f}s), "
            f"avg_wait={self.avg_wait:.2f}s (ema {self.ema_wait:.2f}s)"
        )
        parts.append(
            "Last/peak: "
            f"last_run={self.last_runtime:.2f}s, last_wait={self.last_wait:.2f}s, "
            f"longest_run={self.longest_runtime:.2f}s, longest_wait={self.longest_wait:.2f}s"
        )
        parts.append(f"Tasks completed: {self.tasks_completed}")
        return "\n".join(parts)


__all__ = ["QueueSnapshot"]
