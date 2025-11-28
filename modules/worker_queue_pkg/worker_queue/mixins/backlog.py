from __future__ import annotations

import asyncio
from typing import Optional

from ...instrumented_task import InstrumentedTask

from ..constants import SENTINEL

__all__ = ["BacklogMixin"]


_MAX_DROPPED_TASK_SAMPLES = 5


class BacklogMixin:
    """Backlog management helpers."""

    async def _shed_backlog_if_needed(self, *, trigger: str) -> int:
        if self._backlog_hard_limit is None:
            return 0

        q = self.queue.qsize()
        if q <= self._backlog_hard_limit:
            return 0

        target = self._backlog_shed_to
        if target is None:
            target = self._backlog_high

        target = max(0, target)
        drop_n = max(0, q - target)
        if drop_n == 0:
            return 0

        dropped = 0
        returned_sentinels = 0
        dropped_samples: list[str] = []

        async with self._lock:
            for _ in range(drop_n):
                try:
                    item = self.queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if item is SENTINEL:
                    returned_sentinels += 1
                    self.queue.task_done()
                    continue
                if len(dropped_samples) < _MAX_DROPPED_TASK_SAMPLES:
                    descriptor = self._describe_dropped_item(item)
                    if descriptor:
                        dropped_samples.append(descriptor)
                self._close_enqueued_coroutine(item)
                dropped += 1
                self.queue.task_done()

            for _ in range(returned_sentinels):
                await self.queue.put(SENTINEL)

        self._instrumentation.record_dropped(dropped)
        if dropped:
            details = {
                "Backlog": q,
                "Hard Limit": self._backlog_hard_limit,
                "Dropped": dropped,
                "Trigger": trigger,
                "Target": target,
            }
            details.update(self._queue_state_snapshot())
            if dropped_samples:
                details["Dropped Task Samples"] = "\n".join(dropped_samples)
            self._notifier.warning(
                f"[WorkerQueue:{self._name}] Backlog {q} exceeded hard limit {self._backlog_hard_limit}; dropped {dropped} oldest task(s) (trigger={trigger}).",
                event_key="backlog_shed",
                details=details,
            )
        return dropped

    def _queue_state_snapshot(self) -> dict[str, object]:
        snapshot: dict[str, object] = {}
        try:
            snapshot["Active Workers"] = self._active_workers()
        except Exception:
            pass

        for attr, label in (
            ("_busy_workers", "Busy Workers"),
            ("max_workers", "Max Workers"),
            ("_autoscale_max", "Autoscale Max"),
            ("_baseline_workers", "Baseline Workers"),
            ("_pending_stops", "Pending Stops"),
        ):
            value = getattr(self, attr, None)
            if value is not None:
                snapshot[label] = value

        arrival_tracker = getattr(self, "_arrival_tracker", None)
        if arrival_tracker is not None:
            try:
                snapshot["Arrival Rate/min"] = round(arrival_tracker.rate_per_minute(), 3)
            except Exception:
                pass

        completion_tracker = getattr(self, "_completion_tracker", None)
        if completion_tracker is not None:
            try:
                snapshot["Completion Rate/min"] = round(completion_tracker.rate_per_minute(), 3)
            except Exception:
                pass

        instrumentation = getattr(self, "_instrumentation", None)
        if instrumentation is not None:
            try:
                snapshot["EMA Wait (s)"] = round(float(instrumentation.wait_ema), 3)
            except Exception:
                pass
            try:
                payload = instrumentation.metrics_payload()
            except Exception:
                payload = None
            if isinstance(payload, dict):
                ema_runtime = payload.get("ema_runtime")
                if isinstance(ema_runtime, (int, float)):
                    snapshot["EMA Runtime (s)"] = round(float(ema_runtime), 3)

        return snapshot

    @staticmethod
    def _describe_dropped_item(item) -> Optional[str]:
        if isinstance(item, InstrumentedTask):
            metadata = getattr(item, "metadata", None)
            if metadata is None:
                return repr(item)
            parts: list[str] = []
            if getattr(metadata, "display_name", None):
                parts.append(str(metadata.display_name))
            elif getattr(metadata, "qualname", None):
                parts.append(str(metadata.qualname))
            else:
                parts.append(repr(item))

            meta_bits: list[str] = []
            module = getattr(metadata, "module", None)
            if module:
                meta_bits.append(f"module={module}")
            location = getattr(metadata, "filename", None)
            lineno = getattr(metadata, "first_lineno", None)
            if location:
                if lineno:
                    location = f"{location}:{lineno}"
                meta_bits.append(f"location={location}")

            backlog_at_enqueue = getattr(item, "_backlog_at_enqueue", None)
            if backlog_at_enqueue is not None:
                meta_bits.append(f"backlog_at_enqueue={backlog_at_enqueue}")

            if meta_bits:
                return f"{parts[0]} ({', '.join(meta_bits)})"
            return parts[0]
        try:
            return repr(item)
        except Exception:
            return None
