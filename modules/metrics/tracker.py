from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from . import backend as metrics_backend
from .sanitizer import build_scan_details

DEFAULT_STATUS = "scan_complete"


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


def _sanitize_scan_details(
    *,
    scanner: str,
    source: str,
    accelerated: bool | None,
    flags_count: int,
    scan_result: Mapping[str, Any] | None,
    file_size: int | None,
    scan_duration_ms: int | None,
    frames_scanned: int | None,
    frames_target: int | None,
) -> dict[str, Any]:
    return build_scan_details(
        scanner=scanner,
        source=source,
        accelerated=accelerated,
        flags_count=flags_count,
        scan_result=scan_result,
        file_size=file_size,
        scan_duration_ms=scan_duration_ms,
        frames_scanned=frames_scanned,
        frames_target=frames_target,
    )


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

    def _coerce_frame_count(raw: Any | None) -> int | None:
        if raw is None:
            return None
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return None
        return value if value >= 0 else None

    frames_scanned = None
    frames_target = None
    if isinstance(scan_result, dict):
        frames_scanned = _coerce_frame_count(scan_result.get("video_frames_scanned"))
        frames_target = _coerce_frame_count(scan_result.get("video_frames_target"))

    details = _sanitize_scan_details(
        scanner=scanner,
        source=source,
        accelerated=accelerated,
        flags_count=flags_count,
        scan_result=scan_result,
        file_size=file_size,
        scan_duration_ms=scan_duration_ms,
        frames_scanned=frames_scanned,
        frames_target=frames_target,
    )

    await metrics_backend.accumulate_media_metric(
        occurred_at=occurred,
        guild_id=guild_id,
        content_type=content_type or "unknown",
        status=status or DEFAULT_STATUS,
        was_flagged=was_flagged,
        flags_count=flags_count,
        file_size=file_size,
        scan_duration_ms=scan_duration_ms,
        details=details,
        store_last_details=was_flagged or status != DEFAULT_STATUS,
        accelerated=accelerated,
        frames_scanned=frames_scanned,
        frames_target=frames_target,
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
