from __future__ import annotations

from datetime import timedelta
from typing import Any, Optional

import discord

from modules.metrics import log_media_scan
from modules.utils import mysql


async def record_voice_metrics(
    *,
    guild: discord.Guild,
    channel: discord.VoiceChannel,
    transcript_only: bool,
    high_accuracy: bool,
    listen_delta: timedelta,
    idle_delta: timedelta,
    utterance_count: int,
    status: str,
    report_obj: Optional[Any],
    total_tokens: int,
    request_cost: float,
    usage_snapshot: dict[str, Any],
    duration_ms: int,
    error: Optional[str] = None,
) -> None:
    """Persist moderation metrics for a voice scan cycle."""
    violations_payload: list[dict[str, Any]] = []
    if report_obj and getattr(report_obj, "violations", None):
        for violation in list(report_obj.violations)[:10]:
            violations_payload.append(
                {
                    "user_id": getattr(violation, "user_id", None),
                    "rule": getattr(violation, "rule", None),
                    "reason": getattr(violation, "reason", None),
                    "actions": list(getattr(violation, "actions", []) or []),
                }
            )

    scan_payload: dict[str, Any] = {
        "is_nsfw": bool(violations_payload),
        "reason": status,
        "violations": violations_payload,
        "violations_count": len(violations_payload),
        "total_tokens": int(total_tokens),
        "request_cost_usd": float(request_cost or 0),
        "usage_snapshot": usage_snapshot or {},
        "transcript_only": bool(transcript_only),
        "high_accuracy": bool(high_accuracy),
    }
    if error:
        scan_payload["error"] = error

    extra_context = {
        "status": status,
        "utterance_count": utterance_count,
        "listen_window_seconds": listen_delta.total_seconds(),
        "idle_window_seconds": idle_delta.total_seconds(),
        "transcript_only": bool(transcript_only),
        "high_accuracy": bool(high_accuracy),
    }

    try:
        await log_media_scan(
            guild_id=guild.id,
            channel_id=getattr(channel, "id", None),
            user_id=None,
            message_id=None,
            content_type="voice",
            detected_mime=None,
            filename=None,
            file_size=None,
            source="voice_pipeline",
            scan_result=scan_payload,
            status=status,
            scan_duration_ms=duration_ms,
            accelerated=await mysql.is_accelerated(guild_id=guild.id),
            reference=f"voice:{guild.id}:{getattr(channel, 'id', 'unknown')}",
            extra_context=extra_context,
            scanner="voice_moderation",
        )
    except Exception as metrics_exc:
        print(f"[metrics] Voice metrics logging failed for guild {guild.id}: {metrics_exc}")
