from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Tuple

if TYPE_CHECKING:
    from .downloads import TempDownloadTelemetry


def _coerce_duration(value: Any) -> float | None:
    try:
        duration = float(value)
    except (TypeError, ValueError):
        return None
    if duration <= 0:
        return None
    return duration


def _default_label(step_name: str | None) -> str | None:
    if not step_name:
        return None
    return str(step_name).replace("_", " ").title()


def _coerce_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number


def _coerce_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, (int, float)):
        if value == 1:
            return True
        if value == 0:
            return False
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "y", "on"}:
            return True
        if normalized in {"false", "0", "no", "n", "off"}:
            return False
    return None


def _add_latency_step(
    target: dict[str, dict[str, Any]],
    key: str,
    duration_ms: Any,
    *,
    label: str,
) -> None:
    duration_float = _coerce_duration(duration_ms)
    if duration_float is None or duration_float <= 0:
        return
    entry = target.setdefault(
        key,
        {
            "duration_ms": 0.0,
            "label": label,
        },
    )
    try:
        entry["duration_ms"] = float(entry.get("duration_ms") or 0.0) + duration_float
    except (TypeError, ValueError):
        entry["duration_ms"] = duration_float
    if not entry.get("label"):
        entry["label"] = label


class LatencyTracker:
    """Measure and consolidate latency data for NSFW scans."""

    __slots__ = (
        "_origin_started_at",
        "_execution_started_at",
        "_steps",
        "_queue_label",
        "_queue_name",
    )

    QUEUE_STEP_NAME = "queue_wait"
    DEFAULT_QUEUE_LABEL = "Queue Wait"

    def __init__(
        self,
        *,
        started_at: Any | None = None,
        steps: Any | None = None,
        queue_label: str | None = None,
        queue_name: str | None = None,
    ) -> None:
        self._execution_started_at = time.perf_counter()
        self._origin_started_at = self._coerce_start(started_at)
        if self._origin_started_at is None:
            self._origin_started_at = self._execution_started_at
            queue_wait_ms = 0.0
        else:
            queue_wait_ms = max(
                (self._execution_started_at - self._origin_started_at) * 1000,
                0.0,
            )

        self._steps: dict[str, dict[str, Any]] = normalize_latency_breakdown(steps)
        self._queue_name = queue_name
        resolved_label = queue_label
        if not resolved_label and queue_name:
            pretty = queue_name.replace("_", " ").strip()
            resolved_label = f"{pretty.title()} queue wait" if pretty else None
        self._queue_label = resolved_label or self.DEFAULT_QUEUE_LABEL
        if queue_wait_ms > 0:
            _add_latency_step(
                self._steps,
                self.QUEUE_STEP_NAME,
                queue_wait_ms,
                label=self._queue_label,
            )

    @staticmethod
    def _coerce_start(value: Any | None) -> float | None:
        if value is None:
            return None
        try:
            start = float(value)
        except (TypeError, ValueError):
            return None
        return start

    @property
    def origin_started_at(self) -> float:
        return self._origin_started_at

    @property
    def execution_started_at(self) -> float:
        return self._execution_started_at

    @property
    def steps(self) -> dict[str, dict[str, Any]]:
        return self._steps

    def record_step(
        self,
        key: str,
        duration_ms: Any,
        *,
        label: str | None = None,
    ) -> None:
        resolved_label = label or key.replace("_", " ").title()
        _add_latency_step(self._steps, key, duration_ms, label=resolved_label)

    def record_duration_since(
        self,
        key: str,
        started_at: float,
        *,
        label: str | None = None,
    ) -> None:
        try:
            started_float = float(started_at)
        except (TypeError, ValueError):
            return
        duration = (time.perf_counter() - started_float) * 1000
        self.record_step(key, duration, label=label)

    def merge_steps(self, steps: Any) -> None:
        if not steps:
            return
        self._steps = merge_latency_breakdown(self._steps, steps)

    def total_duration_ms(self) -> float:
        elapsed = (time.perf_counter() - self._origin_started_at) * 1000
        return max(elapsed, 0.0)

    def merge_into_pipeline(
        self,
        pipeline_metrics: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], float]:
        metrics = pipeline_metrics if isinstance(pipeline_metrics, dict) else {}

        existing_total = _coerce_duration(metrics.get("total_latency_ms"))

        metrics["latency_breakdown_ms"] = merge_latency_breakdown(
            metrics.get("latency_breakdown_ms"),
            self._steps,
        )

        total = self.total_duration_ms()
        if existing_total is not None:
            metrics.setdefault("pipeline_total_latency_ms", existing_total)
            total = max(total, existing_total)

        metrics["total_latency_ms"] = float(total)
        metrics["total_duration_ms"] = float(total)
        if self._queue_name:
            metrics.setdefault("queue_name", self._queue_name)
        if self._queue_label and self._queue_label != self.DEFAULT_QUEUE_LABEL:
            metrics.setdefault("queue_label", self._queue_label)
        return metrics, total


def build_download_latency_breakdown(
    telemetry: "TempDownloadTelemetry" | None,
) -> dict[str, dict[str, Any]]:
    steps: dict[str, dict[str, Any]] = {}
    if telemetry is None:
        return steps

    if telemetry.resolve_latency_ms:
        _add_latency_step(
            steps,
            "download_resolve",
            telemetry.resolve_latency_ms,
            label="Resolve Media URL",
        )
    if telemetry.head_latency_ms:
        _add_latency_step(
            steps,
            "download_head",
            telemetry.head_latency_ms,
            label="HEAD Probes",
        )
    if telemetry.download_latency_ms:
        _add_latency_step(
            steps,
            "download_stream",
            telemetry.download_latency_ms,
            label="Download Stream",
        )
    if telemetry.disk_write_latency_ms:
        _add_latency_step(
            steps,
            "download_write",
            telemetry.disk_write_latency_ms,
            label="Disk Writes",
        )

    return steps


@dataclass(slots=True)
class FrameMetrics:
    scanned: Optional[int] = None
    target: Optional[int] = None
    media_total: Optional[int] = None
    processed: Optional[int] = None
    submitted: Optional[int] = None
    pipeline_scanned: Optional[int] = None
    pipeline_target: Optional[int] = None
    dedupe_skipped: Optional[int] = None


@dataclass(slots=True)
class ScanTelemetry:
    total_latency_ms: Optional[float]
    pipeline_metrics: Optional[Dict[str, Any]]
    frame_metrics: FrameMetrics
    frame_lines: List[str] = field(default_factory=list)
    breakdown_lines: List[str] = field(default_factory=list)
    average_latency_per_frame_ms: Optional[float] = None
    bytes_downloaded: Optional[int] = None
    early_exit: Any = None
    accelerated: Optional[bool] = None
    queue_name: Optional[str] = None


def normalize_latency_breakdown(entries: Any) -> dict[str, dict[str, Any]]:
    """Normalise latency breakdown structures into a standard mapping."""

    normalized: dict[str, dict[str, Any]] = {}

    if isinstance(entries, dict):
        iterator: Iterable[tuple[str, Any]] = entries.items()
    elif isinstance(entries, (list, tuple)):
        iterator = []
        for index, entry in enumerate(entries):
            step_name = None
            label = None
            duration_value = None
            if isinstance(entry, dict):
                step_name = entry.get("step")
                label = entry.get("label")
                duration_value = entry.get("duration_ms")
            elif isinstance(entry, (list, tuple)) and entry:
                label = entry[0]
                duration_value = entry[1] if len(entry) > 1 else None

            duration_float = _coerce_duration(duration_value)
            if duration_float is None:
                continue

            key = str(step_name or label or f"step_{index}")
            normalized[key] = {
                "duration_ms": duration_float,
                "label": str(label or step_name or _default_label(key) or key),
            }
        return normalized
    else:
        return normalized

    for step_name, entry in iterator:
        label = None
        duration_value = None
        if isinstance(entry, dict):
            label = entry.get("label")
            duration_value = entry.get("duration_ms")
        else:
            duration_value = entry

        duration_float = _coerce_duration(duration_value)
        if duration_float is None:
            continue

        normalized[str(step_name)] = {
            "duration_ms": duration_float,
            "label": str(label or _default_label(step_name) or step_name),
        }

    return normalized


def merge_latency_breakdown(
    existing: Any,
    new_steps: Dict[str, Dict[str, Any]] | None,
) -> dict[str, dict[str, Any]]:
    """Merge latency breakdown data from a new run into an existing payload."""

    merged = normalize_latency_breakdown(existing)
    if not isinstance(new_steps, dict):
        return merged

    for step_name, entry in new_steps.items():
        duration_float = _coerce_duration(entry.get("duration_ms")) if isinstance(entry, dict) else None
        if duration_float is None:
            continue

        label = None
        if isinstance(entry, dict):
            label = entry.get("label")

        existing_entry = merged.get(step_name)
        if existing_entry:
            previous_duration = _coerce_duration(existing_entry.get("duration_ms"))
            if previous_duration is not None:
                duration_float += previous_duration
            if not label:
                label = existing_entry.get("label")

        merged[str(step_name)] = {
            "duration_ms": duration_float,
            "label": str(label or _default_label(step_name) or step_name),
        }

    return merged


def format_latency_breakdown_lines(entries: Any) -> List[str]:
    """Format latency breakdown data into ordered display lines."""

    normalized = normalize_latency_breakdown(entries)
    sortable: List[Tuple[str, str | None, float]] = []

    for step_name, entry in normalized.items():
        duration = None
        if isinstance(entry, dict):
            duration = _coerce_duration(entry.get("duration_ms"))
        if duration is None:
            continue

        label_value = None
        if isinstance(entry, dict) and entry.get("label") is not None:
            label_value = str(entry.get("label"))

        sortable.append((str(step_name), label_value, duration))

    sortable.sort(key=lambda item: item[2], reverse=True)

    lines: List[str] = []
    for step_name, label, duration in sortable:
        if label and step_name and label != step_name:
            lines.append(f"• {label} (`{step_name}`): {duration:.2f} ms")
        elif label:
            lines.append(f"• {label}: {duration:.2f} ms")
        else:
            lines.append(f"• {step_name}: {duration:.2f} ms")

    return lines


def extract_pipeline_metrics(scan_result: Any) -> dict[str, Any] | None:
    if not isinstance(scan_result, dict):
        return None
    pipeline_metrics = scan_result.get("pipeline_metrics")
    return pipeline_metrics if isinstance(pipeline_metrics, dict) else None


def extract_total_latency_ms(
    pipeline_metrics: Any,
) -> float | None:
    if not isinstance(pipeline_metrics, dict):
        return None
    return _coerce_duration(pipeline_metrics.get("total_latency_ms"))


def extract_frame_metrics(
    scan_result: Any,
    pipeline_metrics: Any,
) -> FrameMetrics:
    if not isinstance(pipeline_metrics, dict):
        pipeline_metrics = None

    if not isinstance(scan_result, dict):
        scan_result = {}

    return FrameMetrics(
        scanned=_coerce_int(scan_result.get("video_frames_scanned")),
        target=_coerce_int(scan_result.get("video_frames_target")),
        media_total=_coerce_int(scan_result.get("video_frames_media_total")),
        processed=_coerce_int((pipeline_metrics or {}).get("frames_processed")),
        submitted=_coerce_int((pipeline_metrics or {}).get("frames_submitted")),
        pipeline_scanned=_coerce_int((pipeline_metrics or {}).get("frames_scanned")),
        pipeline_target=_coerce_int((pipeline_metrics or {}).get("frames_target")),
        dedupe_skipped=_coerce_int((pipeline_metrics or {}).get("dedupe_skipped")),
    )


def format_frame_metrics_lines(metrics: FrameMetrics) -> List[str]:
    lines: List[str] = []

    parts: List[str] = []
    if metrics.scanned is not None or metrics.target is not None:
        scanned_display = str(metrics.scanned or 0)
        if metrics.target is None:
            target_display = "unknown"
        else:
            target_display = str(metrics.target)
        parts.append(f"scan {scanned_display}/{target_display}")
    if metrics.media_total is not None:
        parts.append(f"media total {metrics.media_total}")
    if parts:
        lines.append("Video Frames: " + ", ".join(parts))

    for label, value in (
        ("Processed Frames", metrics.processed),
        ("Submitted Frames", metrics.submitted),
        ("Scanned Frames", metrics.pipeline_scanned),
        ("Target Frames", metrics.pipeline_target),
        ("Dedupe Skipped", metrics.dedupe_skipped),
    ):
        if value is not None:
            lines.append(f"{label}: {value}")

    return lines


def format_video_scan_progress(metrics: FrameMetrics) -> str | None:
    if metrics.scanned is None:
        return None

    scanned_display = str(metrics.scanned)
    if metrics.target is None:
        target_display = "None"
    else:
        target_display = str(metrics.target)

    return f"{scanned_display}/{target_display}"


def compute_average_latency_per_frame(
    total_duration_ms: Any,
    metrics: FrameMetrics,
) -> float | None:
    duration = _coerce_duration(total_duration_ms)
    if duration is None or metrics.scanned is None or metrics.scanned <= 0:
        return None
    return duration / float(metrics.scanned)


def collect_scan_telemetry(
    scan_result: Any,
) -> ScanTelemetry:
    pipeline_metrics = extract_pipeline_metrics(scan_result)
    total_latency_ms = extract_total_latency_ms(pipeline_metrics)
    frame_metrics = extract_frame_metrics(scan_result, pipeline_metrics)

    if isinstance(pipeline_metrics, dict):
        breakdown_source = pipeline_metrics.get("latency_breakdown_ms")
        bytes_downloaded = _coerce_int(pipeline_metrics.get("bytes_downloaded"))
        early_exit = pipeline_metrics.get("early_exit")
        accelerated_flag = _coerce_bool(pipeline_metrics.get("accelerated"))
        queue_name = pipeline_metrics.get("queue_name")
    else:
        breakdown_source = None
        bytes_downloaded = None
        early_exit = None
        accelerated_flag = None
        queue_name = None

    frame_lines = format_frame_metrics_lines(frame_metrics)
    breakdown_lines = format_latency_breakdown_lines(breakdown_source)
    average_latency_per_frame = compute_average_latency_per_frame(
        total_latency_ms,
        frame_metrics,
    )
    if average_latency_per_frame is not None:
        frame_lines.append(f"Average Latency / Frame: {average_latency_per_frame:.2f} ms")

    if (
        frame_metrics.dedupe_skipped is not None
        and frame_metrics.pipeline_scanned is not None
        and frame_metrics.dedupe_skipped >= 0
        and frame_metrics.pipeline_scanned >= 0
    ):
        total_considered = frame_metrics.dedupe_skipped + frame_metrics.pipeline_scanned
        if total_considered > 0:
            dedupe_ratio = (frame_metrics.dedupe_skipped / total_considered) * 100.0
            frame_lines.append(f"Dedupe Savings: {dedupe_ratio:.2f}%")

    return ScanTelemetry(
        total_latency_ms=total_latency_ms,
        pipeline_metrics=pipeline_metrics,
        frame_metrics=frame_metrics,
        frame_lines=frame_lines,
        breakdown_lines=breakdown_lines,
        average_latency_per_frame_ms=average_latency_per_frame,
        bytes_downloaded=bytes_downloaded,
        early_exit=early_exit,
        accelerated=accelerated_flag,
        queue_name=queue_name,
    )
