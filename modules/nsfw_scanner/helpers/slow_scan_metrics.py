from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any, Optional

import discord

log = logging.getLogger(__name__)


def _coerce_positive_float(value: Any) -> Optional[float]:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric) or numeric <= 0:
        return None
    return numeric


def _coerce_non_negative_float(value: Any) -> Optional[float]:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric) or numeric < 0:
        return None
    return numeric


def _format_duration(seconds: float) -> str:
    seconds = float(seconds)
    if seconds < 0:
        seconds = -seconds
    if seconds < 0.001:
        return f"{seconds * 1_000_000:.0f} us"
    if seconds < 1:
        return f"{seconds * 1000:.0f} ms"
    if seconds < 60:
        return f"{seconds:.2f} s"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.2f} min"
    hours = minutes / 60
    return f"{hours:.2f} h"


def _format_rate(rate_per_minute: Optional[float]) -> str:
    if rate_per_minute is None or rate_per_minute <= 0:
        return "n/a"
    return f"{rate_per_minute:.2f}/min"


def _format_percent(value: Optional[float]) -> str:
    if value is None or not math.isfinite(value):
        return "n/a"
    return f"{value * 100:.1f}%"


@dataclass(slots=True)
class SlowScanDiagnostics:
    accelerated: Optional[bool]
    path_line: Optional[str]
    queue_health_lines: list[str]
    queue_rate_lines: list[str]
    processing_rate_lines: list[str]


def _select_queue_snapshots(
    bot: discord.Client,
    accelerated_hint: Optional[bool],
):
    try:
        from cogs.aggregated_moderation.queue_snapshot import QueueSnapshot
    except Exception:
        return None

    cog = bot.get_cog("AggregatedModerationCog") if hasattr(bot, "get_cog") else None
    if cog is None:
        return None

    candidates: list[tuple[str, str]] = []
    if accelerated_hint is True:
        candidates.append(("accelerated_queue", "accelerated"))
    elif accelerated_hint is False:
        candidates.append(("free_queue", "free"))
    else:
        candidates.extend([
            ("free_queue", "free"),
            ("accelerated_queue", "accelerated"),
        ])

    snapshots: list[tuple[str, Any]] = []
    for attr, label in candidates:
        queue = getattr(cog, attr, None)
        if queue is None or not hasattr(queue, "metrics"):
            continue
        try:
            metrics = queue.metrics()
        except Exception:  # noqa: BLE001 - defensive guard
            log.debug("Failed to fetch metrics for %s", attr, exc_info=True)
            continue
        if not isinstance(metrics, dict):
            continue
        try:
            snapshot = QueueSnapshot.from_mapping(metrics)
        except Exception:  # noqa: BLE001
            log.debug("Failed to build queue snapshot for %s", attr, exc_info=True)
            continue
        snapshots.append((label, snapshot))

    if not snapshots:
        return None

    if accelerated_hint is not None:
        return snapshots[0]

    # Choose the snapshot with the highest backlog when no hint is supplied.
    snapshots.sort(key=lambda item: item[1].backlog, reverse=True)
    return snapshots[0]


def _build_queue_health_lines(label: str, snapshot) -> tuple[list[str], list[str]]:
    backlog = snapshot.backlog
    busy = snapshot.busy_workers
    max_workers = snapshot.max_workers or 1
    active = snapshot.active_workers
    baseline = snapshot.baseline_workers
    autoscale_max = snapshot.autoscale_max
    dropped_total = snapshot.dropped_total
    tasks_completed = snapshot.tasks_completed

    wait_avg = _coerce_non_negative_float(snapshot.avg_wait)
    wait_ema = _coerce_non_negative_float(snapshot.ema_wait)
    wait_longest = _coerce_non_negative_float(snapshot.longest_wait)

    runtime_avg = _coerce_non_negative_float(snapshot.avg_runtime)
    runtime_ema = _coerce_non_negative_float(snapshot.ema_runtime)
    runtime_longest = _coerce_non_negative_float(snapshot.longest_runtime)

    queue_lines: list[str] = []
    queue_lines.append(
        (
            f"- Queue `{label}` backlog={backlog} "
            f"(busy {busy}/{max_workers}, active {active}, baseline {baseline}, burst {autoscale_max})"
        )
    )

    watermark_parts: list[str] = []
    if snapshot.backlog_high is not None:
        watermark_parts.append(f"high={snapshot.backlog_high}")
    if snapshot.backlog_low is not None:
        watermark_parts.append(f"low={snapshot.backlog_low}")
    if snapshot.backlog_hard_limit is not None:
        hard_value = str(snapshot.backlog_hard_limit)
        if snapshot.backlog_shed_to is not None:
            hard_value = f"{hard_value}->{snapshot.backlog_shed_to}"
        watermark_parts.append(f"hard={hard_value}")
    if watermark_parts:
        queue_lines.append("- Watermarks " + ", ".join(watermark_parts))

    queue_lines.append(
        f"- Throughput totals completed={tasks_completed:,}, dropped={dropped_total:,}"
    )

    wait_parts: list[str] = []
    if wait_avg is not None:
        wait_parts.append(f"avg {_format_duration(wait_avg)}")
    if wait_ema is not None:
        wait_parts.append(f"ema {_format_duration(wait_ema)}")
    if wait_longest is not None:
        wait_parts.append(f"max {_format_duration(wait_longest)}")
    if wait_parts:
        queue_lines.append("- Wait " + ", ".join(wait_parts))

    runtime_parts: list[str] = []
    if runtime_avg is not None:
        runtime_parts.append(f"avg {_format_duration(runtime_avg)}")
    if runtime_ema is not None:
        runtime_parts.append(f"ema {_format_duration(runtime_ema)}")
    if runtime_longest is not None:
        runtime_parts.append(f"max {_format_duration(runtime_longest)}")
    if runtime_parts:
        queue_lines.append("- Runtime " + ", ".join(runtime_parts))

    wait_value = wait_ema if wait_ema is not None else wait_avg or 0.0
    runtime_value = runtime_ema if runtime_ema is not None else runtime_avg or 0.0
    if runtime_value <= 0:
        runtime_value = max(runtime_longest or 0.0, 0.001)

    observed_throughput = None
    if busy > 0 and runtime_value > 0:
        observed_throughput = (busy / runtime_value) * 60

    capacity_throughput = (max_workers / runtime_value) * 60 if runtime_value > 0 else None

    L = backlog + busy
    total_time = wait_value + runtime_value if wait_value is not None else runtime_value
    arrival_rate = None
    if L > 0 and total_time > 0:
        arrival_rate = (L / total_time) * 60

    load_factor = None
    if observed_throughput and observed_throughput > 0 and arrival_rate is not None:
        load_factor = arrival_rate / observed_throughput
    elif capacity_throughput and capacity_throughput > 0 and arrival_rate is not None:
        load_factor = arrival_rate / capacity_throughput

    backlog_clear_minutes = None
    if backlog > 0 and observed_throughput and observed_throughput > 0:
        backlog_clear_minutes = backlog / observed_throughput

    rate_lines: list[str] = []
    rate_lines.append(
        "- Rates "
        f"lambda~{_format_rate(arrival_rate)} "
        f"mu~{_format_rate(observed_throughput)} "
        f"mu_max~{_format_rate(capacity_throughput)} "
        f"load={_format_percent(load_factor)}"
    )

    if backlog_clear_minutes is not None:
        rate_lines.append(
            "- Backlog clearance ~ "
            f"{_format_duration(backlog_clear_minutes * 60)} at current mu"
        )

    utilization = busy / max_workers if max_workers else 0.0
    rate_lines.append(f"- Utilisation {_format_percent(utilization)}")

    return queue_lines, rate_lines


async def _collect_processing_rate_lines() -> list[str]:
    try:
        from cogs.aggregated_moderation.media_rates import MediaRateCalculator
    except Exception:
        return []

    try:
        calculator = MediaRateCalculator()
        rates = await calculator.compute_rates()
    except Exception:  # noqa: BLE001
        log.debug("Failed to fetch media processing rates", exc_info=True)
        return []

    lines: list[str] = []
    window_minutes = calculator.window_minutes
    lines.append(f"Window {window_minutes:.1f} min lookback")

    if not rates:
        lines.append("No media processed in lookback window.")
        return lines

    total_rate = sum(rate.per_minute for rate in rates)
    total_scans = sum(rate.scans for rate in rates)
    lines.append(f"Total throughput {total_rate:.2f}/min ({total_scans:,} scans)")

    for rate in rates[:5]:
        lines.append(
            f"- {rate.content_type}: {rate.per_minute:.2f}/min ({rate.scans} scans)"
        )

    if len(rates) > 5:
        remaining = len(rates) - 5
        lines.append(f"- ... plus {remaining} additional content types")

    return lines


def _extract_queue_wait_ms(telemetry, total_ms: Optional[float]) -> tuple[Optional[float], Optional[float]]:
    queue_wait_ms = None
    processing_ms = None

    pipeline_metrics = getattr(telemetry, "pipeline_metrics", None)
    if isinstance(pipeline_metrics, dict):
        breakdown = pipeline_metrics.get("latency_breakdown_ms")
        if isinstance(breakdown, dict):
            queue_entry = breakdown.get("queue_wait")
            if isinstance(queue_entry, dict):
                queue_wait_ms = _coerce_non_negative_float(queue_entry.get("duration_ms"))

    if queue_wait_ms is not None and total_ms:
        processing_ms = max(total_ms - queue_wait_ms, 0.0)
    elif total_ms is not None:
        processing_ms = total_ms

    return queue_wait_ms, processing_ms


async def gather_slow_scan_diagnostics(
    bot: discord.Client,
    *,
    telemetry,
    total_ms: Optional[float],
    accelerated_hint: Optional[bool] = None,
) -> SlowScanDiagnostics:
    accelerated_flag = accelerated_hint
    telemetry_flag = getattr(telemetry, "accelerated", None)
    if accelerated_flag is None and isinstance(telemetry_flag, bool):
        accelerated_flag = telemetry_flag

    queue_health_lines: list[str] = []
    queue_rate_lines: list[str] = []
    queue_label: Optional[str] = None

    selected = _select_queue_snapshots(bot, accelerated_flag)
    if selected is not None:
        queue_label, snapshot = selected
        health_lines, rate_lines = _build_queue_health_lines(queue_label, snapshot)
        queue_health_lines.extend(health_lines)
        queue_rate_lines.extend(rate_lines)

    queue_wait_ms, processing_ms = _extract_queue_wait_ms(telemetry, total_ms)

    path_parts: list[str] = []
    if accelerated_flag is not None:
        path_parts.append(f"Accelerated path: {'yes' if accelerated_flag else 'no'}")
    if queue_label is not None:
        path_parts.append(f"Queue: {queue_label}")
    if queue_wait_ms is not None and total_ms:
        queue_share = queue_wait_ms / total_ms if total_ms else None
        processing_share = None
        if processing_ms is not None and total_ms:
            processing_share = processing_ms / total_ms
        path_parts.append(
            f"Queue wait {queue_wait_ms / 1000:.2f}s ({_format_percent(queue_share)})"
        )
        if processing_ms is not None:
            path_parts.append(
                f"Processing {processing_ms / 1000:.2f}s ({_format_percent(processing_share)})"
            )
    elif processing_ms is not None and total_ms:
        processing_share = processing_ms / total_ms
        path_parts.append(
            f"Processing {processing_ms / 1000:.2f}s ({_format_percent(processing_share)})"
        )

    processing_rate_lines = await _collect_processing_rate_lines()

    path_line = " - ".join(path_parts) if path_parts else None

    return SlowScanDiagnostics(
        accelerated=accelerated_flag,
        path_line=path_line,
        queue_health_lines=queue_health_lines,
        queue_rate_lines=queue_rate_lines,
        processing_rate_lines=processing_rate_lines,
    )


__all__ = ["SlowScanDiagnostics", "gather_slow_scan_diagnostics"]
