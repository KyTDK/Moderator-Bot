from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from . import backend as metrics_backend

DEFAULT_STATUS = "scan_complete"


def _sorted_summary(summary: dict[str, Any] | None, limit: int = 5) -> list[dict[str, Any]]:
    if not isinstance(summary, dict):
        return []
    try:
        ordered = sorted(
            summary.items(),
            key=lambda item: float(item[1] or 0),
            reverse=True,
        )
    except Exception:
        ordered = summary.items()
    collection = [
        {"category": str(name), "score": float(score) if isinstance(score, (int, float)) else score}
        for name, score in ordered
    ]
    return collection[:limit]


def _compute_flags_count(scan_result: dict[str, Any] | None) -> int:
    if not isinstance(scan_result, dict):
        return 0
    summary = scan_result.get("summary_categories")
    if isinstance(summary, dict):
        try:
            return sum(
                1
                for score in summary.values()
                if isinstance(score, (int, float)) and float(score) >= 0.5
            )
        except Exception:
            pass
    return 1 if scan_result.get("is_nsfw") else 0


def _build_scan_payload(scan_result: dict[str, Any] | None, *, limit: int = 5) -> dict[str, Any]:
    if not isinstance(scan_result, dict):
        return {}
    primary_keys = {
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
    }
    payload: dict[str, Any] = {}
    for key in primary_keys:
        if key in scan_result:
            payload[key] = scan_result[key]

    summary = payload.get("summary_categories")
    if isinstance(summary, dict):
        payload["top_summary_categories"] = _sorted_summary(summary, limit=limit)

    extras = {k: v for k, v in scan_result.items() if k not in primary_keys}
    if extras:
        payload["extras"] = extras
    return payload


async def log_media_scan(
    *,
    guild_id: int | None,
    channel_id: int | None,
    user_id: int | None,
    message_id: int | None,
    content_type: str,
    detected_mime: str | None,
    filename: str | None,
    file_size: int | None,
    source: str,
    scan_result: dict[str, Any] | None,
    status: str = DEFAULT_STATUS,
    scan_duration_ms: int | None = None,
    accelerated: bool | None = None,
    reference: str | None = None,
    extra_context: dict[str, Any] | None = None,
    scanner: str = "nsfw_scanner",
    occurred_at: datetime | None = None,
) -> None:
    """Accumulate media scan metrics into rollups."""

    status = (status or DEFAULT_STATUS).strip() or DEFAULT_STATUS
    if len(status) > 32:
        status = status[:32]
    flags_count = _compute_flags_count(scan_result)
    was_flagged = bool(scan_result and scan_result.get("is_nsfw"))
    occurred = occurred_at or datetime.now(timezone.utc)

    details: dict[str, Any] = {
        "scanner": scanner,
        "source": source,
        "channel_id": channel_id,
        "user_id": user_id,
        "message_id": message_id,
        "file": {
            "name": filename,
            "mime": detected_mime,
            "size_bytes": file_size,
        },
        "scan": _build_scan_payload(scan_result),
        "accelerated": accelerated,
        "flags_count": flags_count,
    }
    if extra_context:
        details["context"] = extra_context

    await metrics_backend.accumulate_media_metric(
        occurred_at=occurred,
        guild_id=guild_id,
        content_type=content_type or "unknown",
        status=status or DEFAULT_STATUS,
        was_flagged=was_flagged,
        flags_count=flags_count,
        file_size=file_size,
        scan_duration_ms=scan_duration_ms,
        reference=reference or filename,
        details=details,
        store_last_details=was_flagged or status != DEFAULT_STATUS,
        accelerated=accelerated,
    )


async def get_media_metrics_summary(
    *,
    guild_id: int | None = None,
    since: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return aggregate metrics grouped by content type."""

    return await metrics_backend.summarise_rollups(
        guild_id=guild_id,
        since=since,
    )


async def get_media_metric_rollups(
    *,
    guild_id: int | None = None,
    content_type: str | None = None,
    since: datetime | None = None,
    limit: int = 30,
) -> list[dict[str, Any]]:
    """Fetch pre-aggregated daily rollups for dashboards."""

    return await metrics_backend.fetch_metric_rollups(
        guild_id=guild_id,
        content_type=content_type,
        since=since,
        limit=limit,
    )


async def get_media_metric_global_rollups(
    *,
    content_type: str | None = None,
    since: datetime | None = None,
    limit: int = 30,
) -> list[dict[str, Any]]:
    """Fetch daily rollups aggregated across every guild."""

    return await metrics_backend.fetch_global_rollups(
        content_type=content_type,
        since=since,
        limit=limit,
    )


async def get_media_metrics_totals() -> dict[str, Any]:
    """Fetch the global aggregate metrics record."""

    return await metrics_backend.fetch_metric_totals()


__all__ = [
    "log_media_scan",
    "get_media_metrics_summary",
    "get_media_metric_rollups",
    "get_media_metric_global_rollups",
    "get_media_metrics_totals",
]
