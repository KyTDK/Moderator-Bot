from __future__ import annotations

import asyncio
import logging
from typing import Mapping, Optional

import discord

from modules.utils.log_channel import DeveloperLogField, log_to_developer_channel

__all__ = ["QueueEventNotifier"]


class QueueEventNotifier:
    """Dispatch worker queue events to logs and the developer channel."""

    _STDOUT_DETAIL_CHAR_LIMIT = 600
    _STDOUT_DETAIL_EXTRA_LINES = 5

    def __init__(
        self,
        *,
        queue_name: str,
        logger: Optional[logging.Logger] = None,
        developer_bot: Optional[discord.Client] = None,
        developer_context: Optional[str] = None,
        echo_stdout: bool = True,
    ) -> None:
        self._name = queue_name
        self._logger = logger or logging.getLogger(f"{__name__}.{queue_name}")
        self._bot = developer_bot
        self._context = developer_context or f"worker_queue.{queue_name}"
        self._echo_stdout = echo_stdout

    def info(
        self,
        message: str,
        *,
        event_key: str | None = None,
        details: Optional[Mapping[str, object]] = None,
    ) -> None:
        self._log(
            "info",
            message,
            event_key=event_key,
            details=details,
            echo_stdout=self._echo_stdout,
        )

    def warning(
        self,
        message: str,
        *,
        event_key: str | None = None,
        details: Optional[Mapping[str, object]] = None,
    ) -> None:
        self._log(
            "warning",
            message,
            event_key=event_key,
            details=details,
            echo_stdout=self._echo_stdout,
        )

    def error(
        self,
        message: str,
        *,
        event_key: str | None = None,
        details: Optional[Mapping[str, object]] = None,
    ) -> None:
        self._log(
            "error",
            message,
            event_key=event_key,
            details=details,
            echo_stdout=self._echo_stdout,
        )

    def debug(
        self,
        message: str,
        *,
        event_key: str | None = None,
        details: Optional[Mapping[str, object]] = None,
    ) -> None:
        self._log(
            "debug",
            message,
            event_key=event_key,
            details=details,
            echo_stdout=False,
        )

    def _maybe_dispatch(
        self,
        severity: str,
        summary: str,
        *,
        event_key: str | None,
        details: Optional[Mapping[str, object]] = None,
    ) -> None:
        if not self._bot:
            return

        normalized = severity.lower()
        if normalized not in {"warning", "error", "critical"}:
            return

        embed_fields: list[DeveloperLogField] = [
            DeveloperLogField(name="Queue", value=self._name, inline=True),
        ]
        if event_key:
            embed_fields.append(DeveloperLogField(name="Event", value=event_key, inline=True))

        description: Optional[str] = None
        for key, value in (details or {}).items():
            if key.lower() == "description":
                description = str(value)
                continue
            embed_fields.append(
                DeveloperLogField(
                    name=str(key),
                    value=str(value),
                    inline=False,
                )
            )

        embed_fields.append(DeveloperLogField(name="Context", value=self._context, inline=False))

        async def _send() -> None:
            success = await log_to_developer_channel(
                self._bot,
                summary=summary,
                severity=severity,
                context=self._context,
                description=description,
                fields=embed_fields,
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

    def _log(
        self,
        severity: str,
        message: str,
        *,
        event_key: str | None,
        details: Optional[Mapping[str, object]],
        echo_stdout: bool,
    ) -> None:
        log_method = getattr(self._logger, severity, None)
        if log_method is None:
            log_method = self._logger.info
        log_method(message)
        if echo_stdout:
            self._emit_stdout(message, details)
        self._maybe_dispatch(severity, message, event_key=event_key, details=details)

    def _emit_stdout(self, message: str, details: Optional[Mapping[str, object]]) -> None:
        print(message)
        if not details:
            return
        for key, value in details.items():
            formatted = self._format_detail_value(value)
            if not formatted:
                continue
            lines = formatted.splitlines() or [formatted]
            first_line = lines[0]
            print(f"    - {key}: {first_line}")
            extra_lines = lines[1 : 1 + self._STDOUT_DETAIL_EXTRA_LINES]
            for line in extra_lines:
                print(f"      {line}")
            if len(lines) > 1 + len(extra_lines):
                print("      …")

    def _format_detail_value(self, value: object | None) -> str:
        if value is None:
            return ""
        text = str(value)
        if not text:
            return ""
        if len(text) > self._STDOUT_DETAIL_CHAR_LIMIT:
            text = text[: self._STDOUT_DETAIL_CHAR_LIMIT - 1] + "…"
        return text
