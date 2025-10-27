"""Shared helpers for normalizing latency breakdown data."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, List, Sequence


@dataclass(slots=True)
class LatencyEntry:
    """Normalized latency information for a single pipeline step."""

    step: str | None
    label: str | None
    duration_ms: float


def _coerce_duration(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_dict_breakdown(data: dict[str, Any]) -> List[LatencyEntry]:
    entries: list[LatencyEntry] = []
    for step_name, entry in data.items():
        label = None
        duration_source: Any = entry
        if isinstance(entry, dict):
            label = entry.get("label") or entry.get("step")
            duration_source = entry.get("duration_ms")
        duration = _coerce_duration(duration_source)
        if duration is None:
            continue
        entries.append(
            LatencyEntry(
                step=step_name if isinstance(step_name, str) else None,
                label=label if isinstance(label, str) else None,
                duration_ms=duration,
            )
        )
    return entries


def _normalize_sequence_breakdown(data: Iterable[Any]) -> List[LatencyEntry]:
    entries: list[LatencyEntry] = []
    for entry in data:
        step = None
        label = None
        duration_source: Any = None
        if isinstance(entry, dict):
            step_candidate = entry.get("step")
            step = step_candidate if isinstance(step_candidate, str) else None
            label_candidate = entry.get("label")
            label = label_candidate if isinstance(label_candidate, str) else None
            duration_source = entry.get("duration_ms")
        elif isinstance(entry, (list, tuple)) and entry:
            label = entry[0] if isinstance(entry[0], str) else None
            duration_source = entry[1] if len(entry) > 1 else None
        else:
            duration_source = entry
        duration = _coerce_duration(duration_source)
        if duration is None:
            continue
        entries.append(LatencyEntry(step=step, label=label, duration_ms=duration))
    return entries


def normalize_latency_breakdown(breakdown: Any) -> List[LatencyEntry]:
    """Convert a latency breakdown payload into comparable entries."""

    if isinstance(breakdown, dict):
        return _normalize_dict_breakdown(breakdown)
    if isinstance(breakdown, (list, tuple)):
        return _normalize_sequence_breakdown(breakdown)
    return []


def _ensure_entries(
    breakdown: Any | Sequence[LatencyEntry] | Iterable[LatencyEntry],
) -> List[LatencyEntry]:
    if isinstance(breakdown, list) and all(
        isinstance(item, LatencyEntry) for item in breakdown
    ):
        return breakdown
    if isinstance(breakdown, Sequence) and all(
        isinstance(item, LatencyEntry) for item in breakdown
    ):
        return list(breakdown)
    if isinstance(breakdown, Iterable):
        collected = list(breakdown)
        if collected and all(isinstance(item, LatencyEntry) for item in collected):
            return collected
    return normalize_latency_breakdown(breakdown)


def format_latency_breakdown_lines(
    breakdown: Any | Sequence[LatencyEntry] | Iterable[LatencyEntry],
    *,
    min_duration_ms: float = 0.0,
    sort_desc: bool = False,
    bullet: str | None = None,
    decimals: int = 2,
    fallback_label_style: str = "raw",
    include_step_label: bool = False,
    step_wrapper: Callable[[str], str] | None = None,
) -> List[str]:
    """Format latency entries into readable lines for embeds."""

    entries = [
        entry
        for entry in _ensure_entries(breakdown)
        if entry.duration_ms > min_duration_ms
    ]

    if not entries:
        return []

    if sort_desc:
        entries.sort(key=lambda item: item.duration_ms, reverse=True)

    lines: list[str] = []
    for entry in entries:
        label = entry.label
        if not label and entry.step:
            if fallback_label_style == "title":
                label = entry.step.replace("_", " ").title()
            elif fallback_label_style == "raw":
                label = entry.step
        if not label:
            continue

        display_label = label
        if include_step_label and entry.step and entry.step != label:
            formatted_step = step_wrapper(entry.step) if step_wrapper else entry.step
            display_label = f"{label} ({formatted_step})"

        prefix = f"{bullet} " if bullet else ""
        lines.append(
            f"{prefix}{display_label}: {entry.duration_ms:.{decimals}f} ms"
        )

    return lines


def merge_latency_breakdowns(
    *breakdowns: Any | Sequence[LatencyEntry] | Iterable[LatencyEntry],
    fallback_label_style: str = "title",
) -> dict[str, dict[str, Any]]:
    """Combine multiple breakdown payloads into a normalized mapping."""

    merged: dict[str, dict[str, Any]] = {}
    fallback_index = 1

    def _resolve_step(entry: LatencyEntry, label: str | None) -> str:
        nonlocal fallback_index
        if entry.step:
            return entry.step
        if label:
            return label.lower().replace(" ", "_")
        key = f"step_{fallback_index}"
        fallback_index += 1
        return key

    for raw_breakdown in breakdowns:
        for entry in _ensure_entries(raw_breakdown):
            if entry.duration_ms <= 0:
                continue
            label = entry.label
            step_key = _resolve_step(entry, label)
            existing = merged.setdefault(
                step_key,
                {
                    "duration_ms": 0.0,
                    "label": label,
                },
            )
            existing_duration = existing.get("duration_ms") or 0.0
            try:
                existing_duration = float(existing_duration)
            except (TypeError, ValueError):
                existing_duration = 0.0
            existing["duration_ms"] = existing_duration + entry.duration_ms
            if label and not existing.get("label"):
                existing["label"] = label

    for step, payload in merged.items():
        if payload.get("label"):
            continue
        if fallback_label_style == "raw":
            payload["label"] = step
        else:
            payload["label"] = step.replace("_", " ").title()

    return {
        step: {
            "duration_ms": float(data.get("duration_ms") or 0.0),
            "label": data.get("label"),
        }
        for step, data in merged.items()
    }


__all__ = [
    "LatencyEntry",
    "format_latency_breakdown_lines",
    "merge_latency_breakdowns",
    "normalize_latency_breakdown",
]

