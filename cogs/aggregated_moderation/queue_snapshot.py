from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
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
    busy_workers: int
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
    last_runtime_details: Mapping[str, Any]
    longest_runtime_details: Mapping[str, Any]
    check_interval: float
    scale_down_grace: float

    @classmethod
    def from_mapping(cls, metrics: Mapping[str, Any]) -> QueueSnapshot:
        baseline = max(1, _int(metrics.get("baseline_workers"), 1))
        return cls(
            name=str(metrics.get("name") or "queue"),
            backlog=_int(metrics.get("backlog")),
            active_workers=_int(metrics.get("active_workers")),
            busy_workers=_int(metrics.get("busy_workers"))
            if metrics.get("busy_workers") is not None
            else _int(metrics.get("active_workers")),
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
            last_runtime_details=dict(metrics.get("last_runtime_details") or {}),
            longest_runtime_details=dict(metrics.get("longest_runtime_details") or {}),
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
        high = self.backlog_high if self.backlog_high is not None else "-"
        low = self.backlog_low if self.backlog_low is not None else "-"
        parts = [
            f"Backlog: {self.backlog}",
            (
                "Workers: "
                f"busy={self.busy_workers}/{self.max_workers}, "
                f"allocated={self.active_workers}, "
                f"baseline {self.baseline_workers}, burst {self.autoscale_max}"
            ),
            f"Pending stops: {self.pending_stops}",
            f"Watermarks: high={high}, low={low}",
        ]
        if self.backlog_hard_limit is not None:
            limit = str(self.backlog_hard_limit)
            if self.backlog_shed_to is not None:
                limit = f"{limit} -> shed to {self.backlog_shed_to}"
            parts.append(f"Hard limit: {limit}")
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

    @staticmethod
    def _format_wall_time(value: Any) -> Optional[str]:
        try:
            ts = float(value)
        except (TypeError, ValueError):
            return None
        try:
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        except (OSError, OverflowError):
            return None
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")

    @staticmethod
    def _int_or_none(value: Any) -> Optional[int]:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _float_or_none(value: Any) -> Optional[float]:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _format_source(details: Mapping[str, Any]) -> Optional[str]:
        filename = details.get("filename")
        if not filename:
            return None
        try:
            filename = os.path.relpath(str(filename))
        except (TypeError, ValueError):
            filename = str(filename)
        line = details.get("first_lineno")
        if line:
            try:
                return f"{filename}:{int(line)}"
            except (TypeError, ValueError):
                return filename
        return filename

    @staticmethod
    def _format_workers(details: Mapping[str, Any]) -> Optional[str]:
        active = QueueSnapshot._int_or_none(details.get("active_workers_start"))
        busy = QueueSnapshot._int_or_none(details.get("busy_workers_start"))
        max_workers = QueueSnapshot._int_or_none(details.get("max_workers"))
        autoscale = QueueSnapshot._int_or_none(details.get("autoscale_max"))
        if busy is None and active is None and max_workers is None:
            return None
        parts: list[str] = []
        if busy is not None:
            value = str(busy)
            if max_workers is not None:
                value = f"{value}/{max_workers}"
            parts.append(f"busy={value}")
        if active is not None:
            if max_workers is not None:
                parts.append(f"allocated={active}/{max_workers}")
            else:
                parts.append(f"allocated={active}")
        elif max_workers is not None and busy is None:
            parts.append(f"max={max_workers}")
        if autoscale and (max_workers is None or autoscale > max_workers):
            parts.append(f"burst {autoscale}")
        return ", ".join(parts) if parts else None

    @staticmethod
    def _format_backlog(details: Mapping[str, Any]) -> Optional[str]:
        enqueue = QueueSnapshot._int_or_none(details.get("backlog_at_enqueue"))
        start = QueueSnapshot._int_or_none(details.get("backlog_at_start"))
        finish = QueueSnapshot._int_or_none(details.get("backlog_at_finish"))
        values = []
        if enqueue is not None:
            values.append(f"enqueued={enqueue}")
        if start is not None:
            values.append(f"start={start}")
        if finish is not None:
            values.append(f"finish={finish}")
        if not values:
            return None
        return " -> ".join(values)

    def format_longest_runtime_detail(self) -> str:
        return self._format_runtime_detail(self.longest_runtime_details)

    def format_last_runtime_detail(self) -> str:
        return self._format_runtime_detail(self.last_runtime_details)

    def _format_runtime_detail(self, details: Mapping[str, Any]) -> str:
        if not details:
            return "No task details captured yet."

        lines: list[str] = []
        name = details.get("display_name")
        if name:
            lines.append(f"Task: `{name}`")

        module = details.get("module")
        if module:
            lines.append(f"Module: `{module}`")

        source = self._format_source(details)
        if source:
            lines.append(f"Source: `{source}`")

        runtime = details.get("runtime")
        wait = details.get("wait")
        if runtime is not None:
            runtime_val = self._float_or_none(runtime)
            if runtime_val is not None:
                wait_suffix = ""
                wait_val = self._float_or_none(wait)
                if wait_val is not None:
                    wait_suffix = f" (wait {wait_val:.2f}s)"
                lines.append(f"Runtime: {runtime_val:.2f}s{wait_suffix}")

        backlog = self._format_backlog(details)
        if backlog:
            lines.append(f"Queue backlog: {backlog}")

        workers = self._format_workers(details)
        if workers:
            lines.append(f"Workers at start: {workers}")

        started_at = self._format_wall_time(details.get("started_at_wall"))
        completed_at = self._format_wall_time(details.get("completed_at_wall"))
        if started_at:
            lines.append(f"Started: {started_at}")
        if completed_at:
            lines.append(f"Finished: {completed_at}")

        return "\n".join(lines)


__all__ = ["QueueSnapshot"]
