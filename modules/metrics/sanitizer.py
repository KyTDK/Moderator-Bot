from __future__ import annotations

from typing import Any, Mapping

__all__ = [
    "build_scan_details",
    "build_scan_snapshot",
    "sanitize_details_blob",
]


_ALLOWED_SCAN_KEYS = {
    "is_nsfw",
    "category",
    "score",
    "reason",
    "threshold",
    "summary_categories",
    "flagged_any",
    "max_similarity",
    "max_category",
    "high_accuracy",
    "clip_threshold",
    "similarity",
    "video_frames_scanned",
    "video_frames_target",
    "video_frames_media_total",
}

_SUMMARY_LIMIT = 5


def _sorted_summary(summary: Mapping[str, Any], *, limit: int = _SUMMARY_LIMIT) -> list[dict[str, Any]]:
    try:
        ordered = sorted(
            (
                (str(name), float(score))
                for name, score in summary.items()
                if isinstance(score, (int, float))
            ),
            key=lambda item: item[1],
            reverse=True,
        )
    except Exception:
        return []
    collection = [
        {"category": name, "score": score}
        for name, score in ordered
    ]
    return collection[:limit]


def _coerce_int(value: Any | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def build_scan_snapshot(scan_result: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(scan_result, Mapping):
        return {}

    payload: dict[str, Any] = {}
    for key in _ALLOWED_SCAN_KEYS:
        if key in scan_result:
            payload[key] = scan_result[key]

    summary = payload.get("summary_categories")
    if isinstance(summary, Mapping):
        payload["top_summary_categories"] = _sorted_summary(summary)

    return payload


def _build_workload_details(
    *,
    file_size: Any | None,
    scan_duration_ms: Any | None,
    frames_scanned: Any | None,
    frames_target: Any | None,
    frames_media_total: Any | None,
) -> dict[str, Any]:
    workload: dict[str, Any] = {}

    file_size_value = _coerce_int(file_size)
    if file_size_value is not None:
        workload["bytes"] = file_size_value

    duration_value = _coerce_int(scan_duration_ms)
    if duration_value is not None:
        workload["duration_ms"] = duration_value

    frames_scanned_value = _coerce_int(frames_scanned)
    if frames_scanned_value is not None:
        workload["frames_scanned"] = frames_scanned_value

    frames_target_value = _coerce_int(frames_target)
    if frames_target_value is not None:
        workload["frames_target"] = frames_target_value

    frames_media_total_value = _coerce_int(frames_media_total)
    if frames_media_total_value is not None:
        workload["frames_media_total"] = frames_media_total_value

    return workload


def build_scan_details(
    *,
    scanner: str,
    source: str,
    accelerated: bool | None,
    flags_count: int,
    scan_result: Mapping[str, Any] | None,
    file_size: Any | None,
    scan_duration_ms: Any | None,
    frames_scanned: Any | None,
    frames_target: Any | None,
    frames_media_total: Any | None,
) -> dict[str, Any]:
    details: dict[str, Any] = {
        "scanner": str(scanner) if scanner else "",
        "source": str(source) if source else "",
        "accelerated": bool(accelerated) if isinstance(accelerated, bool) else None,
        "flags_count": int(flags_count or 0),
    }

    snapshot = build_scan_snapshot(scan_result)
    if snapshot:
        details["scan"] = snapshot

    workload = _build_workload_details(
        file_size=file_size,
        scan_duration_ms=scan_duration_ms,
        frames_scanned=frames_scanned,
        frames_target=frames_target,
        frames_media_total=frames_media_total,
    )
    if workload:
        details["workload"] = workload

    return sanitize_details_blob(details)


def sanitize_details_blob(raw_details: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(raw_details, Mapping):
        return {}

    sanitized: dict[str, Any] = {}

    scanner = raw_details.get("scanner")
    if isinstance(scanner, str) and scanner:
        sanitized["scanner"] = scanner

    source = raw_details.get("source")
    if isinstance(source, str) and source:
        sanitized["source"] = source

    accelerated = raw_details.get("accelerated")
    if isinstance(accelerated, bool):
        sanitized["accelerated"] = accelerated

    flags_count = _coerce_int(raw_details.get("flags_count"))
    if flags_count is not None:
        sanitized["flags_count"] = flags_count

    snapshot = build_scan_snapshot(raw_details.get("scan"))
    if snapshot:
        sanitized["scan"] = snapshot

    workload_payload = raw_details.get("workload")
    if not isinstance(workload_payload, Mapping):
        workload_payload = {}

    workload = _build_workload_details(
        file_size=workload_payload.get("bytes"),
        scan_duration_ms=workload_payload.get("duration_ms"),
        frames_scanned=workload_payload.get("frames_scanned"),
        frames_target=workload_payload.get("frames_target"),
        frames_media_total=workload_payload.get("frames_media_total"),
    )

    # Include workload keys individually as fallback for legacy payloads.
    legacy_frames_scanned = _coerce_int(raw_details.get("frames_scanned"))
    legacy_frames_target = _coerce_int(raw_details.get("frames_target"))
    legacy_frames_media_total = _coerce_int(raw_details.get("frames_media_total"))
    legacy_bytes = _coerce_int(raw_details.get("file_size"))
    legacy_duration = _coerce_int(raw_details.get("scan_duration_ms"))

    if legacy_bytes is not None:
        workload.setdefault("bytes", legacy_bytes)
    if legacy_duration is not None:
        workload.setdefault("duration_ms", legacy_duration)
    if legacy_frames_scanned is not None:
        workload.setdefault("frames_scanned", legacy_frames_scanned)
    if legacy_frames_target is not None:
        workload.setdefault("frames_target", legacy_frames_target)
    if legacy_frames_media_total is not None:
        workload.setdefault("frames_media_total", legacy_frames_media_total)

    if workload:
        sanitized["workload"] = workload

    return sanitized
