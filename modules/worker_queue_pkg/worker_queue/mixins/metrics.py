from __future__ import annotations

__all__ = ["MetricsMixin"]


class MetricsMixin:
    """Telemetry helpers for worker queue state."""

    def metrics(self) -> dict:
        payload = {
            "name": self._name,
            "running": self.running,
            "backlog": self.queue.qsize(),
            "active_workers": self._active_workers(),
            "busy_workers": self._busy_workers,
            "max_workers": self.max_workers,
            "baseline_workers": self._baseline_workers,
            "autoscale_max": self._autoscale_max,
            "pending_stops": self._pending_stops,
            "backlog_high": self._backlog_high,
            "backlog_low": self._backlog_low,
            "check_interval": self._check_interval,
            "scale_down_grace": self._scale_down_grace,
            "backlog_hard_limit": self._backlog_hard_limit,
            "backlog_shed_to": self._backlog_shed_to,
            "arrival_rate_per_min": self._arrival_tracker.rate_per_minute(),
            "completion_rate_per_min": self._completion_tracker.rate_per_minute(),
            "rate_tracking_window": self._rate_window,
            "adaptive_mode": self._adaptive_mode,
            "adaptive_target_workers": self._adaptive_plan_target,
            "adaptive_baseline_workers": self._adaptive_plan_baseline,
            "adaptive_last_applied": self._last_plan_applied,
        }
        payload.update(self._instrumentation.metrics_payload())
        return payload

    def _record_arrival(self) -> None:
        self._arrival_tracker.record()

    def _record_completion(self) -> None:
        self._completion_tracker.record()
