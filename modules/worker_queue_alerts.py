from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional, TYPE_CHECKING

import discord

from modules.utils.log_channel import send_log_message

if TYPE_CHECKING:
    from modules.worker_queue import TaskMetadata, TaskRuntimeDetail

log = logging.getLogger(__name__)


def _format_wall_clock(timestamp: float) -> str:
    dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    return dt.isoformat(timespec="seconds")


class SingularTaskReporter:
    """Build and dispatch alerts for singular WorkerQueue tasks."""

    def __init__(
        self,
        bot: discord.Client,
        *,
        threshold: float = 30.0,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.bot = bot
        self.threshold = float(threshold)
        self._log = logger or log

    async def __call__(self, detail: "TaskRuntimeDetail", queue_name: str) -> None:
        await self.report(detail, queue_name)

    async def report(self, detail: "TaskRuntimeDetail", queue_name: str) -> None:
        embed = self._build_embed(detail, queue_name)
        content = (
            "⚠️ Slow singular task detected in ``{queue}`` (runtime ``{runtime:.2f}s`` "
            "threshold ``{threshold:.2f}s``)."
        ).format(queue=queue_name, runtime=detail.runtime, threshold=self.threshold)

        if not await send_log_message(
            self.bot,
            content=content,
            embed=embed,
            context=f"singular_task_{queue_name}",
            logger=self._log,
        ):
            self._log.debug(
                "Failed to send singular task report for queue=%s task=%s runtime=%.2fs",
                queue_name,
                detail.metadata.display_name,
                detail.runtime,
            )

    def _build_embed(self, detail: "TaskRuntimeDetail", queue_name: str) -> discord.Embed:
        embed = discord.Embed(
            title="Singular task exceeded runtime threshold",
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc),
        )

        metadata = detail.metadata
        description_lines = [
            f"Task ``{metadata.display_name}`` exceeded the configured threshold.",
            f"Runtime ``{detail.runtime:.2f}s`` (threshold ``{self.threshold:.2f}s``)",
            f"Queue wait ``{detail.wait:.2f}s``",
        ]
        embed.description = "\n".join(description_lines)

        embed.add_field(
            name="Queue context",
            value=(
                f"Queue: ``{queue_name}``\n"
                f"Busy workers at start: ``{detail.busy_workers_start}``\n"
                f"Active workers at start: ``{detail.active_workers_start}``\n"
                f"Configured max/autoscale: ``{detail.max_workers}`` / ``{detail.autoscale_max}``"
            ),
            inline=False,
        )

        embed.add_field(
            name="Backlog snapshot",
            value=(
                "enqueue/start/finish: ``{enqueue}`` / ``{start}`` / ``{finish}``".format(
                    enqueue=detail.backlog_at_enqueue,
                    start=detail.backlog_at_start,
                    finish=detail.backlog_at_finish,
                )
            ),
            inline=False,
        )

        embed.add_field(
            name="Wall clock",
            value=(
                f"Started: ``{_format_wall_clock(detail.started_at_wall)}``\n"
                f"Completed: ``{_format_wall_clock(detail.completed_at_wall)}``"
            ),
            inline=False,
        )

        embed.add_field(
            name="Monotonic timestamps",
            value=(
                f"Enqueued: ``{detail.enqueued_at_monotonic:.2f}``\n"
                f"Started: ``{detail.started_at_monotonic:.2f}``\n"
                f"Completed: ``{detail.completed_at_monotonic:.2f}``"
            ),
            inline=False,
        )

        embed.add_field(
            name="Source",
            value=self._format_metadata(metadata),
            inline=False,
        )

        embed.set_footer(text="WorkerQueue singular task monitor")
        return embed

    @staticmethod
    def _format_metadata(metadata: "TaskMetadata") -> str:
        lines = []
        module = getattr(metadata, "module", None)
        qualname = getattr(metadata, "qualname", None)
        function = getattr(metadata, "function", None)
        filename = getattr(metadata, "filename", None)
        first_lineno = getattr(metadata, "first_lineno", None)

        if module:
            lines.append(f"Module: ``{module}``")
        if qualname:
            lines.append(f"Qualname: ``{qualname}``")
        elif function:
            lines.append(f"Function: ``{function}``")
        if filename:
            location = filename
            if first_lineno:
                location = f"{location}:{first_lineno}"
            lines.append(f"Location: ``{location}``")

        return "\n".join(lines) if lines else "Unavailable"


__all__ = ["SingularTaskReporter"]
