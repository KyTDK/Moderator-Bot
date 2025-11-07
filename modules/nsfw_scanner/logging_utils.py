from __future__ import annotations

import logging
from typing import Any

import discord

from modules.utils.log_channel import DeveloperLogField, log_to_developer_channel

from .constants import LOG_CHANNEL_ID
from .helpers.metrics import ScanTelemetry, collect_scan_telemetry
from .helpers.slow_scan_metrics import gather_slow_scan_diagnostics

log = logging.getLogger(__name__)


SLOW_SCAN_THRESHOLD_MS = 90_000.0


SCAN_REASON_DESCRIPTIONS: dict[str, str] = {
    "openai_moderation": "The OpenAI moderation API completed successfully",
    "openai_moderation_timeout": "The OpenAI moderation API timed out and provided no result",
    "openai_moderation_http_timeout": "The HTTP request to the OpenAI moderation API timed out",
    "openai_moderation_connection_error": "A connection error prevented the OpenAI moderation API from responding",
    "similarity_match": "Similarity matching decided the final outcome before moderation finished",
    "no_frames_extracted": "Video processing could not extract frames for moderation",
    "no_nsfw_frames_detected": "Video scan completed with no NSFW frames detected",
}


SCAN_FAILURE_REASONS: set[str] = {
    "openai_moderation_timeout",
    "openai_moderation_http_timeout",
    "openai_moderation_connection_error",
    "no_frames_extracted",
}


MODERATOR_FAILURE_DESCRIPTIONS: dict[str, str] = {
    "no_key_available": "No API key was available for the request",
    "authentication_error": "Authentication with the OpenAI API failed",
    "rate_limit_error": "OpenAI rate limits prevented the request from completing",
    "api_connection_error": "The connection to the OpenAI API failed",
    "openai_timeout": "The OpenAI moderation API did not respond before timing out",
    "http_timeout": "The HTTP client timed out waiting for the OpenAI response",
    "unexpected_api_error": "An unexpected error occurred while calling the OpenAI API",
    "empty_results": "The OpenAI API returned no usable results",
}


def _format_latency(ms_value: float) -> str:
    seconds = ms_value / 1000.0
    return f"{ms_value:.2f} ms ({seconds:.2f} s)"


async def log_slow_scan_if_needed(
    *,
    bot: discord.Client,
    scan_result: dict[str, Any] | None,
    media_type: str | None,
    detected_mime: str | None,
    total_duration_ms: float | int | None,
    filename: str | None,
    message: discord.Message | None = None,
    threshold_ms: float = SLOW_SCAN_THRESHOLD_MS,
    telemetry: ScanTelemetry | None = None,
) -> None:
    """Emit a structured log entry when a scan exceeds ``threshold_ms``."""

    if not LOG_CHANNEL_ID:
        return

    if telemetry is None:
        telemetry = collect_scan_telemetry(scan_result)
    total_ms = telemetry.total_latency_ms

    if total_ms is None or total_ms < threshold_ms:
        return

    fields: list[DeveloperLogField] = [
        DeveloperLogField(name="Total Latency", value=_format_latency(total_ms), inline=False),
    ]

    media_lines: list[str] = []
    if filename:
        media_lines.append(f"Filename: `{filename}`")
    if media_type:
        media_lines.append(f"Type: `{media_type}`")
    if detected_mime:
        media_lines.append(f"MIME: `{detected_mime}`")
    if media_lines:
        fields.append(
            DeveloperLogField(name="Media", value="\n".join(media_lines)[:1024], inline=False)
        )

    if telemetry.frame_lines:
        fields.append(
            DeveloperLogField(
                name="Frame Metrics",
                value="\n".join(telemetry.frame_lines)[:1024],
                inline=False,
            )
        )

    if telemetry.bytes_downloaded is not None:
        fields.append(
            DeveloperLogField(
                name="Bytes Downloaded",
                value=f"{telemetry.bytes_downloaded:,}",
                inline=True,
            )
        )
    if telemetry.early_exit:
        fields.append(
            DeveloperLogField(
                name="Early Exit",
                value=str(telemetry.early_exit)[:1024],
                inline=True,
            )
        )

    diagnostics = None
    accelerated_hint = getattr(telemetry, "accelerated", None)
    try:
        diagnostics = await gather_slow_scan_diagnostics(
            bot,
            telemetry=telemetry,
            total_ms=total_ms,
            accelerated_hint=accelerated_hint,
        )
    except Exception:  # pragma: no cover - defensive diagnostic capture
        log.debug("Failed to gather slow scan diagnostics", exc_info=True)

    if diagnostics is not None:
        if diagnostics.path_line:
            fields.append(
                DeveloperLogField(
                    name="Pipeline Path",
                    value=diagnostics.path_line[:1024],
                    inline=False,
                )
            )
        if diagnostics.queue_health_lines:
            fields.append(
                DeveloperLogField(
                    name="Queue Health",
                    value="\n".join(diagnostics.queue_health_lines)[:1024],
                    inline=False,
                )
            )
        if diagnostics.queue_rate_lines:
            fields.append(
                DeveloperLogField(
                    name="Queue Rates",
                    value="\n".join(diagnostics.queue_rate_lines)[:1024],
                    inline=False,
                )
            )
        if diagnostics.processing_rate_lines:
            fields.append(
                DeveloperLogField(
                    name="Processing Rates",
                    value="\n".join(diagnostics.processing_rate_lines)[:1024],
                    inline=False,
                )
            )

    failure_detail_lines: list[str] = []

    if isinstance(scan_result, dict):
        outcome_lines: list[str] = []
        if "is_nsfw" in scan_result:
            outcome_lines.append(f"NSFW: {bool(scan_result.get('is_nsfw'))}")
        if scan_result.get("reason"):
            reason_value = scan_result.get("reason")
            reason_line = f"Reason: {reason_value}"
            reason_description = None
            if isinstance(reason_value, str):
                reason_description = SCAN_REASON_DESCRIPTIONS.get(reason_value)
                if reason_description:
                    reason_line = f"{reason_line} — {reason_description}"
                if reason_value in SCAN_FAILURE_REASONS and reason_description:
                    failure_detail_lines.append(reason_description)
            outcome_lines.append(reason_line)
        if scan_result.get("category"):
            outcome_lines.append(f"Category: {scan_result.get('category')}")
        if outcome_lines:
            fields.append(
                DeveloperLogField(
                    name="Scan Outcome",
                    value="\n".join(outcome_lines)[:1024],
                    inline=False,
                )
            )

    if telemetry.breakdown_lines:
        fields.append(
            DeveloperLogField(
                name="Latency Breakdown",
                value="\n".join(telemetry.breakdown_lines)[:1024],
                inline=False,
            )
        )

    moderator_meta_lines: list[str] = []
    payload_info_lines: list[str] = []
    pipeline_metrics = getattr(telemetry, "pipeline_metrics", None)
    if isinstance(pipeline_metrics, dict):
        metadata = pipeline_metrics.get("moderator_metadata")
        if isinstance(metadata, dict):
            attempts = metadata.get("attempts")
            if attempts is not None:
                moderator_meta_lines.append(f"Attempts: {attempts}")
            no_key_waits = metadata.get("no_key_waits")
            if no_key_waits:
                moderator_meta_lines.append(f"No-Key Waits: {no_key_waits}")
            had_successful = metadata.get("had_successful_attempt")
            if had_successful is not None:
                moderator_meta_lines.append(f"Successful Attempt: {bool(had_successful)}")
            failures = metadata.get("failures")
            if isinstance(failures, dict) and failures:
                failure_parts = [
                    f"{name}={count}"
                    for name, count in sorted(failures.items())
                    if count
                ]
                if failure_parts:
                    moderator_meta_lines.append(
                        "Failures: " + ", ".join(failure_parts)
                    )
                for name, count in sorted(failures.items()):
                    if not count:
                        continue
                    description = MODERATOR_FAILURE_DESCRIPTIONS.get(name)
                    if not description:
                        continue
                    if count == 1:
                        failure_detail_lines.append(description)
                    else:
                        failure_detail_lines.append(
                            f"{description} ({count} occurrences)"
                        )
            payload_info = metadata.get("payload_info")
            if isinstance(payload_info, dict) and payload_info:
                input_kind = payload_info.get("input_kind")
                if input_kind:
                    payload_info_lines.append(f"Input Kind: {input_kind}")
                payload_mime = payload_info.get("payload_mime")
                if payload_mime:
                    payload_info_lines.append(f"MIME Sent: `{payload_mime}`")
                source_ext = payload_info.get("source_extension")
                original_format = payload_info.get("original_format")
                source_desc_parts: list[str] = []
                if source_ext:
                    source_desc_parts.append(source_ext)
                if original_format and original_format.lower() != (source_ext or "").strip(" .").lower():
                    source_desc_parts.append(original_format)
                if source_desc_parts:
                    payload_info_lines.append(f"Source Format: {', '.join(source_desc_parts)}")
                image_size = payload_info.get("image_size")
                if isinstance(image_size, (list, tuple)) and len(image_size) == 2:
                    payload_info_lines.append(f"Dimensions: {image_size[0]}×{image_size[1]}")
                image_mode = payload_info.get("image_mode")
                if image_mode:
                    payload_info_lines.append(f"Image Mode: {image_mode}")
                source_bytes = payload_info.get("source_bytes")
                payload_bytes = payload_info.get("payload_bytes")
                byte_parts: list[str] = []
                if isinstance(payload_bytes, (int, float)):
                    byte_parts.append(f"payload={int(payload_bytes):,} B")
                if isinstance(source_bytes, (int, float)) and source_bytes != payload_bytes:
                    byte_parts.append(f"source={int(source_bytes):,} B")
                if byte_parts:
                    payload_info_lines.append("Bytes: " + ", ".join(byte_parts))
                base64_chars = payload_info.get("base64_chars")
                if isinstance(base64_chars, (int, float)):
                    payload_info_lines.append(f"Base64 Size: {int(base64_chars):,} chars")
                conversion_performed = payload_info.get("conversion_performed")
                if conversion_performed:
                    target = payload_info.get("conversion_target") or "unknown"
                    reason = payload_info.get("conversion_reason") or "unspecified"
                    encode_ms = payload_info.get("encode_duration_ms")
                    if isinstance(encode_ms, (int, float)):
                        payload_info_lines.append(
                            f"Conversion: yes → {target} ({reason}, {encode_ms:.2f} ms)"
                        )
                    else:
                        payload_info_lines.append(f"Conversion: yes → {target} ({reason})")
                elif conversion_performed is not None:
                    payload_info_lines.append("Conversion: no")
                request_model = payload_info.get("request_model")
                if request_model:
                    payload_info_lines.append(f"Request Model: {request_model}")
                response_model = payload_info.get("response_model")
                if response_model and response_model != request_model:
                    payload_info_lines.append(f"Response Model: {response_model}")
                response_ms = payload_info.get("response_ms")
                if isinstance(response_ms, (int, float)):
                    payload_info_lines.append(f"API Response ms (provider): {response_ms:.2f}")
                response_id = payload_info.get("response_id")
                if response_id:
                    payload_info_lines.append(f"Response ID: `{response_id}`")
    if moderator_meta_lines:
        fields.append(
            DeveloperLogField(
                name="Moderator Metadata",
                value="\n".join(moderator_meta_lines)[:1024],
                inline=False,
            )
        )
    if payload_info_lines:
        fields.append(
            DeveloperLogField(
                name="Moderator Payload",
                value="\n".join(payload_info_lines)[:1024],
                inline=False,
            )
        )

    if failure_detail_lines:
        deduped_lines = []
        seen = set()
        for line in failure_detail_lines:
            if line in seen:
                continue
            seen.add(line)
            deduped_lines.append(line)
        fields.append(
            DeveloperLogField(
                name="Failure Details",
                value="\n".join(deduped_lines)[:1024],
                inline=False,
            )
        )

    context_lines: list[str] = []
    if message is not None:
        guild = getattr(message, "guild", None)
        if guild is not None:
            guild_name = getattr(guild, "name", "Unknown Guild")
            context_lines.append(f"Guild: {guild_name} (`{getattr(guild, 'id', 'unknown')}`)")
        channel = getattr(message, "channel", None)
        if channel is not None:
            channel_name = getattr(channel, "name", None) or getattr(channel, "id", "Unknown")
            context_lines.append(f"Channel: {channel_name} (`{getattr(channel, 'id', 'unknown')}`)")
        author = getattr(message, "author", None)
        if author is not None:
            author_name = getattr(author, "name", None) or getattr(author, "id", "Unknown")
            context_lines.append(f"Author: {author_name} (`{getattr(author, 'id', 'unknown')}`)")
        if getattr(message, "jump_url", None):
            context_lines.append(f"[Jump to message]({message.jump_url})")

    if context_lines:
        fields.append(
            DeveloperLogField(
                name="Context",
                value="\n".join(context_lines)[:1024],
                inline=False,
            )
        )

    try:
        success = await log_to_developer_channel(
            bot,
            summary="Slow NSFW scan detected",
            severity="warning",
            fields=fields,
            footer=f"Exceeded {threshold_ms / 1000:.0f}s threshold",
            context="nsfw_scanner.slow_scan",
        )
    except Exception:  # pragma: no cover - defensive logging
        log.debug("Failed to send slow scan log", exc_info=True)
        return

    if not success:
        log.debug("Failed to report slow scan to LOG_CHANNEL_ID=%s", LOG_CHANNEL_ID)


__all__ = ["log_slow_scan_if_needed", "SLOW_SCAN_THRESHOLD_MS"]
