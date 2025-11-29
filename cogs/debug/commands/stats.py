from __future__ import annotations

import math
import os
import platform
import time
from typing import Any, Iterable, List, Tuple

import discord
import tracemalloc

from modules.core.health import (
    format_overall_summary,
    format_status_counts,
    get_health_snapshot,
    render_health_lines,
)
from modules.metrics import compute_latency_breakdown
from modules.utils.mysql.connection import get_offline_snapshot_stats

tracemalloc.start()

__all__ = ["build_stats_embed", "collect_top_allocations"]


def collect_top_allocations(show_all: bool, limit: int = 10) -> list[str]:
    snapshot = tracemalloc.take_snapshot()
    top_stats = snapshot.statistics("lineno")
    project_root = os.getcwd()
    allocations: list[str] = []
    for stat in top_stats:
        frame = stat.traceback[0]
        filename = frame.filename
        if not show_all and not filename.startswith(project_root):
            continue
        if filename.startswith(project_root):
            filename = os.path.relpath(filename, project_root)
        avg_size = stat.size // stat.count if stat.count else 0
        allocations.append(
            f"{len(allocations)+1}. {filename}:{frame.lineno} "
            f"- size={stat.size / 1024:.1f} KiB, count={stat.count}, avg={avg_size} B"
        )
        if len(allocations) >= limit:
            break
    return allocations


def _chunk_lines(lines: Iterable[str], max_len: int = 900) -> list[str]:
    chunks: list[str] = []
    current = ""
    for line in lines:
        if len(current) + len(line) + 1 > max_len:
            if current:
                chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


def _truncate_field_value(raw_value: Any, limit: int = 1024) -> str:
    """Clamp embed field values to Discord's 1024-char limit."""
    text = str(raw_value) if raw_value is not None else ""
    if len(text) <= limit:
        return text

    ellipsis = "…"
    if text.startswith("```") and text.endswith("```"):
        suffix = "```"
        newline_idx = text.find("\n", 3)
        if newline_idx == -1:
            prefix = "```"
            inner_start = 3
        else:
            prefix = text[: newline_idx + 1]
            inner_start = newline_idx + 1
        inner = text[inner_start:-3]
        available = max(0, limit - len(prefix) - len(suffix) - len(ellipsis))
        trimmed_inner = inner[:available].rstrip()
        return f"{prefix}{trimmed_inner}{ellipsis}{suffix}"

    available = max(0, limit - len(ellipsis))
    return text[:available].rstrip() + ellipsis


def _add_embed_field(
    embed: discord.Embed,
    *,
    name: str,
    value: Any,
    inline: bool = False,
) -> None:
    embed.add_field(name=name, value=_truncate_field_value(value), inline=inline)


def _format_duration(seconds: float | None) -> str:
    if not seconds or seconds <= 0:
        return "unknown"
    if seconds < 1:
        return f"{seconds * 1000:.0f} ms"
    if seconds < 60:
        return f"{seconds:.2f} s"
    minutes, rem = divmod(seconds, 60)
    if minutes < 60:
        return f"{int(minutes)}m {rem:.0f}s"
    hours, minutes = divmod(minutes, 60)
    return f"{int(hours)}h {int(minutes)}m"


def _format_bytes(size_bytes: int | None) -> str:
    if size_bytes is None or size_bytes < 0:
        return "unknown"
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    size = float(size_bytes)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size_bytes} B"


def _build_backup_summary(debug_texts: dict[str, Any]) -> str | None:
    stats = get_offline_snapshot_stats()
    if stats is None:
        return debug_texts.get("backup_missing", "No local backup information is available.")

    completed = stats.completed_at.strftime("%Y-%m-%d %H:%M:%S UTC")
    duration = _format_duration(stats.duration_seconds)
    size = _format_bytes(stats.size_bytes)
    template = debug_texts.get(
        "backup_value",
        (
            "**Completed:** {completed}\n"
            "**Duration:** {duration}\n"
            "**Tables:** {tables}\n"
            "**Rows:** {rows}\n"
            "**Size:** {size}\n"
            "**Location:** `{path}`"
        ),
    )
    return template.format(
        completed=completed,
        duration=duration,
        tables=f"{stats.table_count:,}",
        rows=f"{stats.row_count:,}",
        size=size,
        path=stats.db_path,
    )


async def build_stats_embed(cog, interaction: discord.Interaction, show_all: bool) -> discord.Embed:
    guild_id = interaction.guild.id if interaction.guild else None
    debug_texts = cog.bot.translate("cogs.debug.embed", guild_id=guild_id)

    current, peak = tracemalloc.get_traced_memory()
    current_mb = current / 1024 / 1024
    peak_mb = peak / 1024 / 1024

    rss = cog.process.memory_info().rss / 1024 / 1024
    vms = cog.process.memory_info().vms / 1024 / 1024

    cpu_percent = cog.process.cpu_percent(interval=0.5)
    uptime = time.time() - cog.start_time
    uptime_str = time.strftime("%H:%M:%S", time.gmtime(uptime))

    threads = cog.process.num_threads()
    handles = cog.process.num_handles() if hasattr(cog.process, "num_handles") else "N/A"

    top_allocations = collect_top_allocations(show_all)
    if not top_allocations:
        top_allocations.append(cog.bot.translate("cogs.debug.no_allocations", guild_id=guild_id))

    embed = discord.Embed(
        title=debug_texts["title"],
        color=discord.Color.blurple(),
    )
    _add_embed_field(
        embed,
        name=debug_texts["memory_name"],
        value=debug_texts["memory_value"].format(
            rss=rss,
            vms=vms,
            current_mb=current_mb,
            peak_mb=peak_mb,
        ),
        inline=False,
    )
    _add_embed_field(
        embed,
        name=debug_texts["cpu_name"],
        value=debug_texts["cpu_value"].format(cpu_percent=cpu_percent, threads=threads, handles=handles),
        inline=False,
    )
    _add_embed_field(
        embed,
        name=debug_texts["bot_name"],
        value=debug_texts["bot_value"].format(
            guilds=len(cog.bot.guilds),
            users=len(cog.bot.users),
            uptime=uptime_str,
        ),
        inline=False,
    )

    for index, chunk in enumerate(_chunk_lines(top_allocations), start=1):
        _add_embed_field(
            embed,
            name=debug_texts["allocations_name"].format(index=index),
            value=f"```{chunk}```",
            inline=False,
        )

    try:
        latency_table = await _build_latency_table()
    except Exception as exc:  # noqa: BLE001
        latency_error = debug_texts.get("latency_error", "Unable to fetch latency metrics ({error})")
        _add_embed_field(
            embed,
            name=debug_texts.get("latency_name", "Latency / Coverage"),
            value=latency_error.format(error=exc),
            inline=False,
        )
    else:
        if latency_table:
            _add_embed_field(
                embed,
                name=debug_texts.get("latency_name", "Latency / Coverage"),
                value=f"```\n{latency_table}\n```",
                inline=False,
            )

    embed.set_footer(text=debug_texts["footer"].format(host=platform.node(), python_version=platform.python_version()))
    try:
        queue_lines, rate_lines = _collect_worker_summaries(cog)
    except Exception as exc:  # noqa: BLE001
        _add_embed_field(
            embed,
            name=debug_texts["worker_name"],
            value=debug_texts["worker_error"].format(error=exc),
            inline=False,
        )
    else:
        if queue_lines:
            _add_embed_field(
                embed,
                name=debug_texts["worker_name"],
                value=f"```\n" + "\n".join(queue_lines) + "\n```",
                inline=False,
            )
        if rate_lines:
            _add_embed_field(
                embed,
                name=cog.bot.translate("cogs.debug.worker_rates", guild_id=guild_id),
                value=f"```\n" + "\n".join(rate_lines) + "\n```",
                inline=False,
            )

    backup_summary = _build_backup_summary(debug_texts)
    if backup_summary:
        _add_embed_field(
            embed,
            name=debug_texts.get("backup_name", "Local MySQL Backup"),
            value=backup_summary,
            inline=False,
        )

    health_snapshot = get_health_snapshot()
    overall_line = format_overall_summary(health_snapshot)
    health_counts = format_status_counts(health_snapshot, include_ok=True, show_percent=True)
    health_lines = "\n".join(
        render_health_lines(health_snapshot, include_ok=True, per_status_limit=None)
    )
    sections = [overall_line, health_counts, health_lines]
    health_value = "\n\n".join(section for section in sections if section)

    _add_embed_field(
        embed,
        name=debug_texts.get("health_name", "System health"),
        value=health_value,
        inline=False,
    )

    return embed


async def _build_latency_table() -> str:
    breakdown = await compute_latency_breakdown()
    rows: list[tuple[str, str, str, str, str, str]] = []

    overall_row = _build_latency_row("Overall", breakdown.get("overall"))
    if overall_row:
        rows.append(overall_row)

    by_type = breakdown.get("by_type", {})
    ordered_types = sorted(
        by_type.items(),
        key=lambda item: (item[1].get("scans") or 0),
        reverse=True,
    )
    for content_type, payload in ordered_types:
        label = content_type.replace("_", " ").title()
        row = _build_latency_row(label, payload)
        if row:
            rows.append(row)

    if not rows:
        return ""
    return _render_latency_table(rows)


def _build_latency_row(label: str, payload: Any) -> tuple[str, str, str, str, str, str] | None:
    if not isinstance(payload, dict):
        return None
    overall = payload.get("average_latency_ms")
    acceleration = payload.get("acceleration") or {}
    free_bucket = acceleration.get("non_accelerated") or {}
    accel_bucket = acceleration.get("accelerated") or {}

    free_latency = free_bucket.get("average_latency_ms")
    accelerated_latency = accel_bucket.get("average_latency_ms")

    free_coverage = free_bucket.get("frame_coverage_rate")
    accel_coverage = accel_bucket.get("frame_coverage_rate")

    return (
        label,
        _format_latency_value(overall),
        _format_latency_value(free_latency),
        _format_coverage_value(free_coverage),
        _format_latency_value(accelerated_latency),
        _format_coverage_value(accel_coverage),
    )


def _render_latency_table(rows: list[tuple[str, ...]]) -> str:
    headers = ("Type", "Overall", "Free", "Free Cov", "Accel", "Accel Cov")
    full_rows = [headers, *rows]
    col_count = len(headers)
    widths = [
        max(len(row[idx]) for row in full_rows)
        for idx in range(col_count)
    ]
    lines: list[str] = []
    for row in full_rows:
        parts = [
            row[idx].ljust(widths[idx])
            for idx in range(col_count)
        ]
        lines.append(" ".join(parts).rstrip())
    return "\n".join(lines)


def _format_latency_value(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return "n/a"
    if not math.isfinite(numeric):
        return "n/a"
    return f"{numeric:,.1f}"


def _format_coverage_value(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return "n/a"
    if not math.isfinite(numeric):
        return "n/a"
    return f"{numeric * 100:.1f}%"


def _collect_worker_summaries(cog) -> Tuple[List[str], List[str]]:
    queue_lines: list[str] = []
    rate_lines: list[str] = []

    def fmt_line(cog_name: str, queue_name: str, queue_obj) -> Tuple[str, str]:
        metrics_callable = getattr(queue_obj, "metrics", None)
        data = metrics_callable() if callable(metrics_callable) else None
        if not data:
            backlog = getattr(getattr(queue_obj, "queue", None), "qsize", lambda: "?")()
            workers = len(getattr(queue_obj, "workers", []))
            max_workers = getattr(queue_obj, "max_workers", "?")
            summary = f"[{cog_name}:{queue_name}] backlog={backlog} workers={workers}/{max_workers}"
            return summary, summary

        def _int(value, default=0):
            try:
                if value is None:
                    return default
                return int(value)
            except (TypeError, ValueError):
                return default

        def _float(value, default=0.0):
            try:
                if value is None:
                    return default
                return float(value)
            except (TypeError, ValueError):
                return default

        backlog = _int(data.get("backlog"))
        max_workers = max(1, _int(data.get("max_workers"), 1))
        busy = _int(data.get("busy_workers"), _int(data.get("active_workers")))
        baseline = max(1, _int(data.get("baseline_workers"), 1))
        burst = _int(data.get("autoscale_max"), max_workers)
        hi_value = data.get("backlog_high")
        lo_value = data.get("backlog_low")
        hi = str(_int(hi_value)) if hi_value is not None else "-"
        lo = str(_int(lo_value)) if lo_value is not None else "-"
        pending = _int(data.get("pending_stops"))
        tasks_completed = _int(data.get("tasks_completed"))
        dropped = _int(data.get("dropped_tasks_total"))
        limit = None
        hard_limit_value = data.get("backlog_hard_limit")
        if hard_limit_value is not None:
            hard_limit = _int(hard_limit_value)
            shed_to_value = data.get("backlog_shed_to")
            limit = f"{hard_limit}->{_int(shed_to_value)}" if shed_to_value is not None else str(hard_limit)
        wait_avg = _float(data.get("avg_wait_time"))
        wait_last = _float(data.get("last_wait_time"))
        wait_long = _float(data.get("longest_wait"))
        run_avg = _float(data.get("avg_runtime"))
        run_last = _float(data.get("last_runtime"))
        run_long = _float(data.get("longest_runtime"))
        running_flag = bool(data.get("running"))
        arrival_rate = _float(data.get("arrival_rate_per_min"))
        completion_rate = _float(data.get("completion_rate_per_min"))
        adaptive_mode = bool(data.get("adaptive_mode"))
        adaptive_target = _int(data.get("adaptive_target_workers"), max_workers)
        adaptive_baseline = _int(data.get("adaptive_baseline_workers"), baseline)
        rate_window = _float(data.get("rate_tracking_window"), 0.0)

        parts = [
            f"[{cog_name}:{queue_name}]",
            f"backlog={backlog}",
            f"busy={busy}/{max_workers}",
            f"base={baseline}",
            f"target={adaptive_target}" if adaptive_mode else f"burst={burst}",
            f"hi={hi}",
            f"lo={lo}",
            f"pend={pending}",
            f"tasks={tasks_completed}",
            f"drop={dropped}",
            f"wait={wait_avg:.2f}|{wait_last:.2f}|{wait_long:.2f}",
            f"run={run_avg:.2f}|{run_last:.2f}|{run_long:.2f}",
            f"running={running_flag}",
        ]
        if limit is not None:
            parts.insert(7, f"limit={limit}")
        summary_line = " ".join(parts)

        if rate_window > 0:
            window_minutes = rate_window / 60.0
            window_part = f"{window_minutes:.1f}m" if rate_window >= 60 else f"{rate_window:.0f}s"
        else:
            window_part = "n/a"
        worker_descriptor = (
            f"target={adaptive_target} baseline={adaptive_baseline}"
            if adaptive_mode
            else f"max={max_workers} baseline={baseline}"
        )
        rate_line = (
            f"{queue_name}@{cog_name}: req={arrival_rate:.2f}/min "
            f"proc={completion_rate:.2f}/min {worker_descriptor} window={window_part}"
        )
        return summary_line, rate_line

    for cog_name in ("AggregatedModerationCog", "EventDispatcherCog", "ScamDetectionCog"):
        cog_instance = getattr(cog.bot, "get_cog", lambda name: None)(cog_name)
        if not cog_instance:
            continue
        for queue_attr in ("free_queue", "accelerated_queue", "video_queue"):
            queue_obj = getattr(cog_instance, queue_attr, None)
            if queue_obj is None:
                continue
            queue_name = queue_attr.replace("_queue", "")
            summary, rate_line = fmt_line(cog_name, queue_name, queue_obj)
            queue_lines.append(summary)
            rate_lines.append(rate_line)

    return queue_lines, rate_lines
