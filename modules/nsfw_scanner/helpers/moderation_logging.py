from __future__ import annotations

import logging
from typing import Any, Optional

from modules.utils.log_channel import DeveloperLogField, log_to_developer_channel

from ..constants import LOG_CHANNEL_ID
from .moderation_state import ImageModerationState

__all__ = [
    "truncate_text",
    "report_moderation_fallback_to_log",
    "report_remote_payload_failure",
]

log = logging.getLogger(__name__)


def truncate_text(value: str, limit: int = 1024) -> str:
    if not isinstance(value, str) or len(value) <= limit:
        return value
    return value[: limit - 1] + "\u2026"


async def report_remote_payload_failure(
    scanner,
    *,
    attempt_number: int,
    max_attempts: int,
    error_message: str,
    image_state: ImageModerationState | None,
    payload_metadata: dict[str, Any] | None,
    latency_snapshot: dict[str, Any] | None,
    context_summary: str | None = None,
) -> None:
    bot = getattr(scanner, "bot", None)
    if bot is None:
        return

    metadata: dict[str, Any] | None = payload_metadata if isinstance(payload_metadata, dict) else None
    if metadata is not None:
        if metadata.get("remote_failure_reported"):
            return
        metadata["remote_failure_reported"] = True

    description = truncate_text(error_message, 1024)
    fields: list[DeveloperLogField] = [
        DeveloperLogField(name="Attempt", value=f"{attempt_number}/{max_attempts}", inline=True),
    ]
    if LOG_CHANNEL_ID:
        fields.append(DeveloperLogField(name="Log Channel ID", value=str(LOG_CHANNEL_ID), inline=True))
    if isinstance(image_state, ImageModerationState):
        log_details = []
        try:
            log_details = image_state.logging_details()
        except Exception:
            log_details = []
        if image_state.source_url:
            fields.append(
                DeveloperLogField(
                    name="Image URL",
                    value=truncate_text(image_state.source_url, 1024),
                    inline=False,
                )
            )
        if log_details:
            fields.append(
                DeveloperLogField(
                    name="Image details",
                    value=truncate_text(" | ".join(log_details), 1024),
                    inline=False,
                )
            )

    context_lines: list[str] = []
    if metadata:
        for key in (
            "guild_id",
            "channel_id",
            "message_id",
            "message_jump_url",
            "source_url",
            "moderation_payload_strategy",
        ):
            value = metadata.get(key)
            if value is None:
                continue
            context_lines.append(f"{key}={value}")
        tracker_snapshot = metadata.get("moderation_tracker")
        if isinstance(tracker_snapshot, dict) and tracker_snapshot:
            context_lines.append(f"tracker={tracker_snapshot}")

    if latency_snapshot:
        latency_parts: list[str] = []
        attempts = latency_snapshot.get("attempts")
        if attempts:
            latency_parts.append(f"attempts={attempts}")
        failures = latency_snapshot.get("failures")
        if failures:
            latency_parts.append(f"failures={failures}")
        timings = latency_snapshot.get("timings_ms")
        if timings:
            latency_parts.append(f"timings_ms={timings}")
        payload_info = latency_snapshot.get("payload_details")
        if payload_info:
            latency_parts.append(f"payload_details={payload_info}")
        if latency_parts:
            context_lines.append("latency_snapshot=" + " | ".join(latency_parts))

    if context_summary:
        context_lines.append(f"context={context_summary}")

    if context_lines:
        fields.append(
            DeveloperLogField(
                name="Context",
                value=truncate_text("\n".join(context_lines), 1024),
                inline=False,
            )
        )

    try:
        success = await log_to_developer_channel(
            bot,
            summary="Moderator API remote payload failed",
            severity="error",
            description=description,
            fields=fields,
            timestamp=True,
            context="nsfw_scanner.moderation_remote",
        )
    except Exception:
        log.debug(
            "Failed to report remote payload failure to LOG_CHANNEL_ID=%s",
            LOG_CHANNEL_ID,
            exc_info=True,
        )
        return

    if not success:
        log.debug(
            "Failed to report remote payload failure to LOG_CHANNEL_ID=%s",
            LOG_CHANNEL_ID,
        )


async def report_moderation_fallback_to_log(
    scanner,
    *,
    fallback_notice: str,
    image_state: ImageModerationState,
    payload_metadata: dict[str, Any] | None,
) -> None:
    bot = getattr(scanner, "bot", None)
    if bot is None:
        return

    metadata: dict[str, Any] | None = payload_metadata if isinstance(payload_metadata, dict) else None
    if metadata is not None and metadata.get("fallback_notice_reported"):
        return

    if metadata is not None:
        metadata["fallback_notice_reported"] = True

    fields: list[DeveloperLogField] = []

    events = sorted(image_state.fallback_events)
    if events:
        fields.append(
            DeveloperLogField(
                name="Fallback events",
                value=truncate_text(", ".join(events)),
                inline=False,
            )
        )

    if metadata:
        guild_id = metadata.get("guild_id")
        if guild_id is not None:
            fields.append(DeveloperLogField(name="Guild ID", value=str(guild_id), inline=True))

        channel_id = metadata.get("channel_id")
        if channel_id is not None:
            fields.append(DeveloperLogField(name="Channel ID", value=str(channel_id), inline=True))

        strategy = metadata.get("moderation_payload_strategy")
        if strategy:
            fields.append(DeveloperLogField(name="Payload strategy", value=str(strategy), inline=True))

        jump_url = metadata.get("message_jump_url")
        message_id = metadata.get("message_id")
        if jump_url:
            fields.append(
                DeveloperLogField(
                    name="Message",
                    value=truncate_text(f"[Jump to message]({jump_url})"),
                    inline=False,
                )
            )
        elif message_id is not None:
            fields.append(DeveloperLogField(name="Message ID", value=str(message_id), inline=False))

        source_url = metadata.get("source_url")
        if source_url:
            fields.append(
                DeveloperLogField(
                    name="Source URL",
                    value=truncate_text(source_url),
                    inline=False,
                )
            )

        tracker_snapshot = metadata.get("moderation_tracker")
        if isinstance(tracker_snapshot, dict):
            attempt_parts: list[str] = []
            attempts = tracker_snapshot.get("attempts")
            if attempts:
                attempt_parts.append(f"attempts={attempts}")
            no_key_waits = tracker_snapshot.get("no_key_waits")
            if no_key_waits:
                attempt_parts.append(f"no_key_waits={no_key_waits}")
            if attempt_parts:
                fields.append(
                    DeveloperLogField(
                        name="Attempt stats",
                        value=truncate_text(" | ".join(attempt_parts)),
                        inline=False,
                    )
                )

            failures = tracker_snapshot.get("failures") or {}
            if failures:
                failure_summary = ", ".join(
                    f"{reason}:{count}" for reason, count in sorted(failures.items())
                )
                fields.append(
                    DeveloperLogField(
                        name="Failure breakdown",
                        value=truncate_text(failure_summary),
                        inline=False,
                    )
                )

            payload_info = tracker_snapshot.get("payload_details") or {}
            if payload_info:
                payload_summary = " | ".join(
                    f"{key}={payload_info[key]}" for key in sorted(payload_info.keys())
                )
                fields.append(
                    DeveloperLogField(
                        name="Payload metadata",
                        value=truncate_text(payload_summary),
                        inline=False,
                    )
                )

            timings = tracker_snapshot.get("timings_ms") or {}
            if timings:
                timing_summary = ", ".join(
                    f"{key}:{round(value, 1)}"
                    for key, value in sorted(timings.items(), key=lambda item: item[1], reverse=True)[:4]
                    if value
                )
                if timing_summary:
                    fields.append(
                        DeveloperLogField(
                            name="Timing breakdown (ms)",
                            value=truncate_text(timing_summary),
                            inline=False,
                        )
                    )

        fallback_contexts = metadata.get("fallback_contexts")
        if fallback_contexts:
            fields.append(
                DeveloperLogField(
                    name="Fallback context",
                    value=truncate_text("\n".join(str(item) for item in fallback_contexts)),
                    inline=False,
                )
            )

    try:
        payload_details = image_state.logging_details()
    except Exception:
        payload_details = []

    if payload_details:
        fields.append(
            DeveloperLogField(
                name="Payload details",
                value=truncate_text(" | ".join(payload_details)),
                inline=False,
            )
        )

    try:
        success = await log_to_developer_channel(
            bot,
            summary="Moderator API fallback triggered",
            severity="warning",
            description=fallback_notice,
            fields=fields,
            context="nsfw_scanner.moderation_fallback",
        )
    except Exception:
        log.debug(
            "Failed to report moderation fallback to LOG_CHANNEL_ID=%s", LOG_CHANNEL_ID, exc_info=True
        )
        return

    if not success:
        log.debug("Failed to report moderation fallback to LOG_CHANNEL_ID=%s", LOG_CHANNEL_ID)
