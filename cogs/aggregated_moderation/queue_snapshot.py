from __future__ import annotations

import os
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional


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
        runtime = self.runtime_signal()
        if runtime > 0.0:
            checks = (
                (self.avg_wait, max(5.0, runtime * 2.0)),
                (self.last_wait, max(10.0, runtime * 2.5)),
                (self.longest_wait, max(15.0, runtime * 3.0)),
            )
            return any(value is not None and value >= threshold for value, threshold in checks)
        return self.wait_signal() >= 10.0

    def runtime_signal(self) -> float:
        """Representative runtime derived from available metrics."""
        return self._first_positive(
            (self.avg_runtime, self.ema_runtime, self.last_runtime, self.longest_runtime)
        )

    def wait_signal(self) -> float:
        """Peak wait seen on this queue."""
        return self._max_positive((self.avg_wait, self.ema_wait, self.last_wait, self.longest_wait))

    def backlog_recovered(self) -> bool:
        """True when backlog has fallen back to acceptable bounds."""
        if self.backlog <= 0:
            return True
        if self.backlog_low is not None and self.backlog <= self.backlog_low:
            return True
        return self.backlog <= self.baseline_workers

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
    def _first_positive(values: Iterable[Any]) -> float:
        for value in values:
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                continue
            if numeric > 0:
                return numeric
        return 0.0

    @staticmethod
    def _max_positive(values: Iterable[Any]) -> float:
        best = 0.0
        for value in values:
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                continue
            if numeric > best:
                best = numeric
        return best

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

def _weighted_average(pairs: Iterable[tuple[Optional[float], int]]) -> float:
    total_weight = 0
    total_value = 0.0
    fallback: list[float] = []
    for value, weight in pairs:
        if value is None:
            continue
        value = float(value)
        fallback.append(value)
        if weight and weight > 0:
            total_value += value * weight
            total_weight += weight
    if total_weight > 0:
        return total_value / total_weight
    if fallback:
        return sum(fallback) / len(fallback)
    return 0.0


def _detail_timestamp(detail: Mapping[str, Any]) -> float:
    for key in ("completed_at_wall", "completed_at_monotonic", "started_at_wall", "started_at_monotonic"):
        value = detail.get(key)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return 0.0


def _select_latest_detail(snapshots: Iterable[QueueSnapshot], attr: str) -> Mapping[str, Any]:
    best_detail: Mapping[str, Any] = {}
    best_ts = float("-inf")
    for snapshot in snapshots:
        detail = getattr(snapshot, attr)
        if not detail:
            continue
        ts = _detail_timestamp(detail)
        if ts > best_ts:
            best_ts = ts
            best_detail = detail
    return best_detail


def _select_longest_detail(snapshots: Iterable[QueueSnapshot]) -> Mapping[str, Any]:
    best_detail: Mapping[str, Any] = {}
    best_runtime = float("-inf")
    for snapshot in snapshots:
        detail = snapshot.longest_runtime_details
        runtime = snapshot.longest_runtime
        if detail and runtime > best_runtime:
            best_runtime = runtime
            best_detail = detail
    return best_detail


def merge_queue_snapshots(name: str, snapshots: Iterable[QueueSnapshot]) -> QueueSnapshot:
    """Combine multiple queue snapshots into a single aggregated view."""
    snapshot_list = [snapshot for snapshot in snapshots if snapshot is not None]
    if not snapshot_list:
        raise ValueError("merge_queue_snapshots requires at least one snapshot")
    if len(snapshot_list) == 1:
        snapshot = snapshot_list[0]
        return snapshot if snapshot.name == name else replace(snapshot, name=name)

    def sum_attr(attr: str) -> int:
        return sum(getattr(snapshot, attr) for snapshot in snapshot_list)

    def sum_optional(attr: str) -> Optional[int]:
        values = [getattr(snapshot, attr) for snapshot in snapshot_list if getattr(snapshot, attr) is not None]
        return sum(values) if values else None

    tasks_completed = sum_attr("tasks_completed")
    avg_runtime = _weighted_average((snapshot.avg_runtime, snapshot.tasks_completed) for snapshot in snapshot_list)
    avg_wait = _weighted_average((snapshot.avg_wait, snapshot.tasks_completed) for snapshot in snapshot_list)
    ema_runtime = _weighted_average((snapshot.ema_runtime, snapshot.tasks_completed) for snapshot in snapshot_list)
    ema_wait = _weighted_average((snapshot.ema_wait, snapshot.tasks_completed) for snapshot in snapshot_list)

    last_detail = _select_latest_detail(snapshot_list, "last_runtime_details")
    longest_detail = _select_longest_detail(snapshot_list)

    longest_runtime = max((snapshot.longest_runtime for snapshot in snapshot_list), default=0.0)
    longest_wait = max((snapshot.longest_wait for snapshot in snapshot_list), default=0.0)
    last_runtime = float(last_detail.get("runtime", 0.0)) if last_detail else max(
        (snapshot.last_runtime for snapshot in snapshot_list), default=0.0
    )
    last_wait = float(last_detail.get("wait", 0.0)) if last_detail else max(
        (snapshot.last_wait for snapshot in snapshot_list), default=0.0
    )

    return QueueSnapshot(
        name=name,
        backlog=sum_attr("backlog"),
        active_workers=sum_attr("active_workers"),
        busy_workers=sum_attr("busy_workers"),
        max_workers=sum_attr("max_workers"),
        baseline_workers=sum_attr("baseline_workers"),
        autoscale_max=sum_attr("autoscale_max"),
        pending_stops=sum_attr("pending_stops"),
        backlog_high=sum_optional("backlog_high"),
        backlog_low=sum_optional("backlog_low"),
        backlog_hard_limit=sum_optional("backlog_hard_limit"),
        backlog_shed_to=sum_optional("backlog_shed_to"),
        dropped_total=sum_attr("dropped_total"),
        tasks_completed=tasks_completed,
        avg_runtime=avg_runtime,
        avg_wait=avg_wait,
        ema_runtime=ema_runtime,
        ema_wait=ema_wait,
        last_runtime=last_runtime,
        last_wait=last_wait,
        longest_runtime=longest_runtime,
        longest_wait=longest_wait,
        last_runtime_details=dict(last_detail),
        longest_runtime_details=dict(longest_detail),
        check_interval=max(snapshot.check_interval for snapshot in snapshot_list),
        scale_down_grace=max(snapshot.scale_down_grace for snapshot in snapshot_list),
    )


__all__ = ["QueueSnapshot", "merge_queue_snapshots"]
