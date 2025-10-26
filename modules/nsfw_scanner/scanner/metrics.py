from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import discord

from modules.metrics import log_media_scan

from ..context import GuildScanContext
from ..helpers.downloads import DownloadResult
from .work_item import MediaWorkItem

log = logging.getLogger(__name__)


def queue_media_metrics(
    *,
    context: GuildScanContext,
    message: discord.Message | None,
    actor: discord.Member | None,
    item: MediaWorkItem,
    duration_ms: int,
    result: dict[str, Any] | None,
    detected_mime: str | None,
    file_type: str | None,
    status: str,
    download: Optional[DownloadResult] = None,
) -> None:
    channel_id = getattr(getattr(message, "channel", None), "id", None) if message else None
    user_id = getattr(actor, "id", None) if actor else None
    message_id = getattr(message, "id", None) if message else None

    extra_context = {
        "status": status,
        "detected_mime": detected_mime,
        "file_type": file_type,
        "source": item.source,
        "label": item.label,
    }
    if download is not None:
        extra_context["download_bytes"] = download.bytes_downloaded
    if message_id:
        extra_context["message_id"] = message_id

    async def _log() -> None:
        try:
            await log_media_scan(
                guild_id=context.guild_id,
                channel_id=channel_id,
                user_id=user_id,
                message_id=message_id,
                content_type=file_type or "unknown",
                detected_mime=detected_mime,
                filename=item.label,
                file_size=download.bytes_downloaded if download else None,
                source=item.source,
                scan_result=result,
                status=status,
                scan_duration_ms=duration_ms,
                accelerated=context.limits.is_premium,
                reference=f"{message_id}:{item.label}" if message_id else item.label,
                extra_context=extra_context,
            )
        except Exception as metrics_exc:
            log.debug("Failed to record NSFW metrics for %s: %s", item.label, metrics_exc)

    asyncio.create_task(_log())


__all__ = ["queue_media_metrics"]
