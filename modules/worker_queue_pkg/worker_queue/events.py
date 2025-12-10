from __future__ import annotations

from typing import Optional

from ..notifier import QueueEventNotifier

__all__ = ["QueueEventLogger"]


class QueueEventLogger:
    """Lightweight helper for emitting structured worker queue events."""

    def __init__(self, *, name: str, notifier: QueueEventNotifier) -> None:
        self._name = name
        self._notifier = notifier

    def scaled_up(self, *, old: int, new: int, reason: Optional[str] = None) -> None:
        reason_text = reason or "resize"
        self._notifier.info(
            f"[WorkerQueue:{self._name}] scaled up {old}->{new} (reason={reason_text})",
            event_key=f"scale_up:{new}",
        )

    def scaled_down(self, *, old: int, new: int, reason: Optional[str] = None) -> None:
        reason_text = reason or "resize"
        self._notifier.info(
            f"[WorkerQueue:{self._name}] scaled down {old}->{new} (reason={reason_text})",
            event_key=f"scale_down:{new}",
        )

    def adaptive_plan_updated(
        self,
        *,
        changes: list[str],
        target: int,
        baseline: int,
        backlog_high: Optional[int],
    ) -> None:
        event_key = f"adaptive_plan:{target}:{baseline}:{backlog_high or 'none'}"
        change_summary = ", ".join(changes)
        self._notifier.debug(
            f"[WorkerQueue:{self._name}] adaptive plan updated: {change_summary}",
            event_key=event_key,
        )
