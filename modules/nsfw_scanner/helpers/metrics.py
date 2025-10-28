from __future__ import annotations

from typing import Any, Dict, Iterable


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

