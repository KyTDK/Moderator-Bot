from __future__ import annotations

from typing import Any

import discord

from modules.nsfw_scanner.helpers.metrics import ScanTelemetry, format_video_scan_progress
from modules.nsfw_scanner.utils.file_types import FILE_TYPE_LABELS
from modules.utils.localization import TranslateFn, localize_message

from .localization import (
    REPORT_BASE,
    localize_boolean,
    localize_category,
    localize_decision,
    localize_field_name,
    localize_reason,
)

__all__ = ["build_verbose_scan_embed"]


def _build_payload_lines(
    *,
    payload_info: dict[str, Any] | None,
    translator: TranslateFn | None,
    guild_id: int | None,
) -> list[str]:
    if not isinstance(payload_info, dict) or not payload_info:
        return []

    def _format_int(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _format_int_with_commas(value: Any) -> str | None:
        coerced = _format_int(value)
        return f"{coerced:,}" if coerced is not None else None

    lines: list[str] = []
    resized_flag = payload_info.get("payload_resized")
    if isinstance(resized_flag, bool):
        lines.append(f"Resized: {localize_boolean(translator, resized_flag, guild_id)}")
    elif resized_flag is not None:
        lines.append(f"Resized: {resized_flag}")

    original_size = payload_info.get("image_size")
    payload_width = _format_int(payload_info.get("payload_width"))
    payload_height = _format_int(payload_info.get("payload_height"))
    if payload_width is not None and payload_height is not None:
        if isinstance(original_size, (list, tuple)) and len(original_size) == 2:
            orig_width = _format_int(original_size[0])
            orig_height = _format_int(original_size[1])
            if orig_width is not None and orig_height is not None:
                lines.append(
                    f"Dimensions: {orig_width}x{orig_height} -> {payload_width}x{payload_height}"
                )
            else:
                lines.append(f"Dimensions: {payload_width}x{payload_height}")
        else:
            lines.append(f"Dimensions: {payload_width}x{payload_height}")
    elif isinstance(original_size, (list, tuple)) and len(original_size) == 2:
        orig_width = _format_int(original_size[0])
        orig_height = _format_int(original_size[1])
        if orig_width is not None and orig_height is not None:
            lines.append(f"Dimensions: {orig_width}x{orig_height}")

    source_bytes_fmt = _format_int_with_commas(payload_info.get("source_bytes"))
    payload_bytes_fmt = _format_int_with_commas(payload_info.get("payload_bytes"))
    if payload_bytes_fmt and source_bytes_fmt and source_bytes_fmt != payload_bytes_fmt:
        lines.append(f"Bytes: {source_bytes_fmt} -> {payload_bytes_fmt}")
    elif payload_bytes_fmt:
        lines.append(f"Bytes: {payload_bytes_fmt}")
    elif source_bytes_fmt:
        lines.append(f"Bytes: {source_bytes_fmt}")

    if "payload_edge_limit" in payload_info:
        edge_limit_value = payload_info.get("payload_edge_limit")
        edge_limit_int = _format_int(edge_limit_value)
        if edge_limit_int is not None:
            lines.append(f"Edge Limit: {edge_limit_int:,} px")
        else:
            lines.append("Edge Limit: disabled")

    target_bytes_fmt = _format_int_with_commas(payload_info.get("payload_target_bytes"))
    if target_bytes_fmt:
        lines.append(f"Target Bytes: {target_bytes_fmt}")

    strategy = payload_info.get("payload_strategy")
    if strategy:
        lines.append(f"Strategy: {strategy}")

    quality = payload_info.get("payload_quality")
    if quality is not None:
        lines.append(f"Quality: {quality}")

    return lines


def build_verbose_scan_embed(
    *,
    translator: TranslateFn | None,
    guild_id: int | None,
    author: discord.abc.User | None,
    message,
    filename: str,
    file_type: str,
    detected_mime: str | None,
    decision_key: str,
    scan_result: dict[str, Any] | None,
    telemetry: ScanTelemetry,
    total_latency_ms: int,
) -> discord.Embed:
    actor = author or getattr(message, "author", None)
    actor_id = getattr(actor, "id", None)
    actor_mention = getattr(actor, "mention", None)
    if actor_mention is None and actor_id is not None:
        actor_mention = f"<@{actor_id}>"
    if actor_mention is None:
        actor_mention = localize_message(
            translator,
            REPORT_BASE,
            "description.unknown_user",
            fallback="Unknown user",
            guild_id=guild_id,
        )

    decision_label = localize_decision(translator, decision_key, guild_id)
    file_type_label = localize_message(
        translator,
        REPORT_BASE,
        f"file_types.{file_type}",
        fallback=FILE_TYPE_LABELS.get(file_type, detected_mime or file_type.title()),
        guild_id=guild_id,
    )

    embed = discord.Embed(
        title=localize_message(
            translator,
            REPORT_BASE,
            "title",
            fallback="NSFW Scan Report",
            guild_id=guild_id,
        ),
        description="\n".join(
            [
                localize_message(
                    translator,
                    REPORT_BASE,
                    "description.user",
                    placeholders={"user": actor_mention},
                    fallback="User: {user}",
                    guild_id=guild_id,
                ),
                localize_message(
                    translator,
                    REPORT_BASE,
                    "description.file",
                    placeholders={"filename": filename},
                    fallback="File: `{filename}`",
                    guild_id=guild_id,
                ),
                localize_message(
                    translator,
                    REPORT_BASE,
                    "description.type",
                    placeholders={"file_type": file_type_label},
                    fallback="Type: `{file_type}`",
                    guild_id=guild_id,
                ),
                localize_message(
                    translator,
                    REPORT_BASE,
                    "description.decision",
                    placeholders={"decision": decision_label},
                    fallback="Decision: **{decision}**",
                    guild_id=guild_id,
                ),
            ]
        ),
        color=(
            discord.Color.orange()
            if decision_key == "safe"
            else (
                discord.Color.red()
                if decision_key == "nsfw"
                else discord.Color.dark_grey()
            )
        ),
    )

    embed.add_field(
        name=localize_field_name(translator, "latency_ms", guild_id),
        value=f"{total_latency_ms} ms",
        inline=True,
    )
    if telemetry.breakdown_lines:
        embed.add_field(
            name=localize_field_name(translator, "latency_breakdown", guild_id),
            value="\n".join(telemetry.breakdown_lines)[:1024],
            inline=False,
        )

    pipeline_metrics = telemetry.pipeline_metrics if isinstance(
        getattr(telemetry, "pipeline_metrics", None), dict
    ) else None
    payload_info = None
    if pipeline_metrics:
        moderator_metadata = pipeline_metrics.get("moderator_metadata")
        if isinstance(moderator_metadata, dict):
            payload_info = moderator_metadata.get("payload_info")
    payload_lines = _build_payload_lines(
        payload_info=payload_info,
        translator=translator,
        guild_id=guild_id,
    )
    if payload_lines:
        embed.add_field(
            name=localize_field_name(translator, "payload_details", guild_id),
            value="\n".join(payload_lines)[:1024],
            inline=False,
        )

    if scan_result:
        reason_value = localize_reason(translator, scan_result.get("reason"), guild_id)
        if reason_value:
            embed.add_field(
                name=localize_field_name(translator, "reason", guild_id),
                value=str(reason_value)[:1024],
                inline=False,
            )
        if scan_result.get("category"):
            embed.add_field(
                name=localize_field_name(translator, "category", guild_id),
                value=localize_category(
                    translator,
                    str(scan_result.get("category")),
                    guild_id,
                ),
                inline=True,
            )
        if scan_result.get("score") is not None:
            embed.add_field(
                name=localize_field_name(translator, "score", guild_id),
                value=f"{float(scan_result.get('score') or 0):.3f}",
                inline=True,
            )
        if scan_result.get("flagged_any") is not None:
            embed.add_field(
                name=localize_field_name(translator, "flagged_any", guild_id),
                value=localize_boolean(
                    translator,
                    bool(scan_result.get("flagged_any")),
                    guild_id,
                ),
                inline=True,
            )
        if scan_result.get("summary_categories") is not None:
            embed.add_field(
                name=localize_field_name(translator, "summary_categories", guild_id),
                value=str(scan_result.get("summary_categories")),
                inline=False,
            )
        if scan_result.get("max_similarity") is not None:
            embed.add_field(
                name=localize_field_name(translator, "max_similarity", guild_id),
                value=f"{float(scan_result.get('max_similarity') or 0):.3f}",
                inline=True,
            )
        if scan_result.get("max_category") is not None:
            embed.add_field(
                name=localize_field_name(translator, "max_category", guild_id),
                value=str(scan_result.get("max_category")),
                inline=True,
            )
        if scan_result.get("similarity") is not None:
            embed.add_field(
                name=localize_field_name(translator, "similarity", guild_id),
                value=f"{float(scan_result.get('similarity') or 0):.3f}",
                inline=True,
            )
        if scan_result.get("high_accuracy") is not None:
            embed.add_field(
                name=localize_field_name(translator, "high_accuracy", guild_id),
                value=localize_boolean(
                    translator,
                    bool(scan_result.get("high_accuracy")),
                    guild_id,
                ),
                inline=True,
            )
        if scan_result.get("clip_threshold") is not None:
            embed.add_field(
                name=localize_field_name(translator, "clip_threshold", guild_id),
                value=f"{float(scan_result.get('clip_threshold') or 0):.3f}",
                inline=True,
            )
        if scan_result.get("threshold") is not None:
            try:
                embed.add_field(
                    name=localize_field_name(translator, "moderation_threshold", guild_id),
                    value=f"{float(scan_result.get('threshold') or 0):.3f}",
                    inline=True,
                )
            except Exception:
                pass

    frame_metrics = telemetry.frame_metrics
    if frame_metrics.scanned is not None:
        progress_value = format_video_scan_progress(frame_metrics)
        embed.add_field(
            name=localize_field_name(translator, "video_frames", guild_id),
            value=str(progress_value)[:1024] if progress_value else "?",
            inline=True,
        )
        if telemetry.average_latency_per_frame_ms is not None:
            embed.add_field(
                name=localize_field_name(translator, "average_latency_per_frame_ms", guild_id),
                value=f"{telemetry.average_latency_per_frame_ms:.2f} ms/frame",
                inline=True,
            )

    avatar = getattr(actor, "display_avatar", None)
    avatar_url = getattr(avatar, "url", None) if avatar else None
    if avatar_url:
        embed.set_thumbnail(url=avatar_url)

    return embed
