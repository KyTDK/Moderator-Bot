from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import discord

from modules.utils.log_channel import log_to_developer_channel

__all__ = ["QueueEventNotifier"]


class QueueEventNotifier:
    """Dispatch worker queue events to logs and the developer channel."""

    def __init__(
        self,
        *,
        queue_name: str,
        logger: Optional[logging.Logger] = None,
        developer_bot: Optional[discord.Client] = None,
        developer_context: Optional[str] = None,
        cooldown: float = 30.0,
        echo_stdout: bool = True,
    ) -> None:
        self._name = queue_name
        self._logger = logger or logging.getLogger(f"{__name__}.{queue_name}")
        self._bot = developer_bot
        self._context = developer_context or f"worker_queue.{queue_name}"
        self._cooldown = max(0.0, float(cooldown))
        self._last_sent: dict[str, float] = {}
        self._echo_stdout = echo_stdout

    def info(self, message: str, *, event_key: str | None = None) -> None:
        self._logger.info(message)
        if self._echo_stdout:
            print(message)
        self._maybe_dispatch("info", message, event_key=event_key)

    def warning(self, message: str, *, event_key: str | None = None) -> None:
        self._logger.warning(message)
        if self._echo_stdout:
            print(message)
        self._maybe_dispatch("warning", message, event_key=event_key)

    def error(self, message: str, *, event_key: str | None = None) -> None:
        self._logger.error(message)
        if self._echo_stdout:
            print(message)
        self._maybe_dispatch("error", message, event_key=event_key)

    def debug(self, message: str, *, event_key: str | None = None) -> None:
        self._logger.debug(message)
        self._maybe_dispatch("debug", message, event_key=event_key)

    def _maybe_dispatch(self, severity: str, summary: str, *, event_key: str | None) -> None:
        if not self._bot:
            return

        key = event_key or summary
        now = time.monotonic()
        last_sent = self._last_sent.get(key, 0.0)
        if self._cooldown > 0 and (now - last_sent) < self._cooldown:
            return
        self._last_sent[key] = now

        async def _send() -> None:
            success = await log_to_developer_channel(
                self._bot,
                summary=summary,
                severity=severity,
                context=self._context,
            )
            if not success:
                self._logger.debug(
                    "Failed to dispatch developer log for %s (severity=%s)",
                    self._name,
                    severity,
                )

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop: best-effort synchronous log only.
            return

        loop.create_task(_send())
