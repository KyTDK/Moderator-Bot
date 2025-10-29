from __future__ import annotations

import logging
from typing import Any

import discord

from modules.utils.log_channel import send_log_message

from .constants import LOG_CHANNEL_ID
from .helpers.metrics import ScanTelemetry, collect_scan_telemetry

log = logging.getLogger(__name__)


SLOW_SCAN_THRESHOLD_MS = 30_000.0


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
        telemetry = collect_scan_telemetry(
            scan_result,
            fallback_total_ms=total_duration_ms,
        )
    total_ms = telemetry.total_latency_ms

    if total_ms is None or total_ms < threshold_ms:
        return

    embed = discord.Embed(
        title="Slow NSFW scan detected",
        color=discord.Color.orange(),
    )
    embed.add_field(
        name="Total Latency",
        value=_format_latency(total_ms),
        inline=False,
    )

    media_lines: list[str] = []
    if filename:
        media_lines.append(f"Filename: `{filename}`")
    if media_type:
        media_lines.append(f"Type: `{media_type}`")
    if detected_mime:
        media_lines.append(f"MIME: `{detected_mime}`")
    if media_lines:
        embed.add_field(name="Media", value="\n".join(media_lines)[:1024], inline=False)

    if telemetry.frame_lines:
        embed.add_field(
            name="Frame Metrics",
            value="\n".join(telemetry.frame_lines)[:1024],
            inline=False,
        )

    if telemetry.bytes_downloaded is not None:
        embed.add_field(
            name="Bytes Downloaded",
            value=f"{telemetry.bytes_downloaded:,}",
            inline=True,
        )
    if telemetry.early_exit:
        embed.add_field(
            name="Early Exit",
            value=str(telemetry.early_exit)[:1024],
            inline=True,
        )

    if isinstance(scan_result, dict):
        outcome_lines: list[str] = []
        if "is_nsfw" in scan_result:
            outcome_lines.append(f"NSFW: {bool(scan_result.get('is_nsfw'))}")
        if scan_result.get("reason"):
            outcome_lines.append(f"Reason: {scan_result.get('reason')}")
        if scan_result.get("category"):
            outcome_lines.append(f"Category: {scan_result.get('category')}")
        if outcome_lines:
            embed.add_field(
                name="Scan Outcome",
                value="\n".join(outcome_lines)[:1024],
                inline=False,
            )

    if telemetry.breakdown_lines:
        embed.add_field(
            name="Latency Breakdown",
            value="\n".join(telemetry.breakdown_lines)[:1024],
            inline=False,
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
        embed.add_field(
            name="Context",
            value="\n".join(context_lines)[:1024],
            inline=False,
        )

    embed.set_footer(text=f"Exceeded {threshold_ms / 1000:.0f}s threshold")

    try:
        success = await send_log_message(
            bot,
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
            context="nsfw_scanner.slow_scan",
        )
    except Exception:  # pragma: no cover - defensive logging
        log.debug("Failed to send slow scan log", exc_info=True)
        return

    if not success:
        log.debug("Failed to report slow scan to LOG_CHANNEL_ID=%s", LOG_CHANNEL_ID)


__all__ = ["log_slow_scan_if_needed", "SLOW_SCAN_THRESHOLD_MS"]

