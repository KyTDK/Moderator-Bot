from __future__ import annotations

import asyncio
from typing import Optional

from ..constants import SENTINEL

__all__ = ["BacklogMixin"]


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
                self._close_enqueued_coroutine(item)
                dropped += 1
                self.queue.task_done()

            for _ in range(returned_sentinels):
                await self.queue.put(SENTINEL)

        self._instrumentation.record_dropped(dropped)
        if dropped:
            self._notifier.warning(
                f"[WorkerQueue:{self._name}] Backlog {q} exceeded hard limit {self._backlog_hard_limit}; dropped {dropped} oldest task(s) (trigger={trigger}).",
                event_key="backlog_shed",
                details={
                    "Backlog": q,
                    "Hard Limit": self._backlog_hard_limit,
                    "Dropped": dropped,
                    "Trigger": trigger,
                    "Target": target,
                },
            )
        return dropped
