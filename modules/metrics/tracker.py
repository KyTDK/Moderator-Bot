from __future__ import annotations

from datetime import datetime
from typing import Any

from modules.metrics.models import ModerationMetric
from modules.utils.mysql import metrics as mysql_metrics

MEDIA_EVENT = "media_scan"


def _sorted_summary(summary: dict[str, Any] | None) -> list[dict[str, Any]]:
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
    return [
        {"category": str(name), "score": float(score) if isinstance(score, (int, float)) else score}
        for name, score in ordered
    ]


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


def _build_scan_payload(scan_result: dict[str, Any] | None) -> dict[str, Any]:
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
    scan_duration_ms: int | None = None,
    accelerated: bool | None = None,
    reference: str | None = None,
    extra_context: dict[str, Any] | None = None,
    scanner: str = "nsfw_scanner",
    occurred_at: datetime | None = None,
) -> int:
    """Record a rich media scan metric."""

    flags_count = _compute_flags_count(scan_result)
    was_flagged = bool(scan_result and scan_result.get("is_nsfw"))
    primary_reason = scan_result.get("reason") if isinstance(scan_result, dict) else None

    details: dict[str, Any] = {
        "file": {
            "filename": filename,
            "mime": detected_mime,
            "size_bytes": file_size,
        },
        "scan": _build_scan_payload(scan_result),
        "flags_breakdown": _sorted_summary(
            scan_result.get("summary_categories") if isinstance(scan_result, dict) else None
        ),
        "accelerated": accelerated,
    }
    if extra_context:
        details["context"] = extra_context

    metric_kwargs = {
        "event_type": MEDIA_EVENT,
        "content_type": content_type,
        "guild_id": guild_id,
        "channel_id": channel_id,
        "user_id": user_id,
        "message_id": message_id,
        "was_flagged": was_flagged,
        "flags_count": flags_count,
        "primary_reason": primary_reason,
        "details": details,
        "scan_duration_ms": scan_duration_ms,
        "scanner": scanner,
        "source": source,
        "reference": reference or filename,
    }
    if occurred_at is not None:
        metric_kwargs["occurred_at"] = occurred_at

    metric = ModerationMetric(**metric_kwargs)
    return await mysql_metrics.insert_moderation_metric(metric)


async def get_recent_media_metrics(
    *,
    guild_id: int | None = None,
    limit: int = 100,
    since: datetime | None = None,
) -> list[dict[str, Any]]:
    """Fetch recent media scan metrics for dashboards or diagnostics."""

    return await mysql_metrics.fetch_recent_metrics(
        guild_id=guild_id,
        limit=limit,
        since=since,
    )


async def get_media_metrics_summary(
    *,
    guild_id: int | None = None,
    since: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return aggregate metrics grouped by content type."""

    return await mysql_metrics.summarise_metrics(
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

    return await mysql_metrics.fetch_metric_rollups(
        guild_id=guild_id,
        content_type=content_type,
        since=since,
        limit=limit,
    )


__all__ = [
    "log_media_scan",
    "get_recent_media_metrics",
    "get_media_metrics_summary",
    "get_media_metric_rollups",
]
