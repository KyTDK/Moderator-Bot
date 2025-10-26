from __future__ import annotations

from typing import Any, Optional

import discord

from modules.utils import mod_logging
from modules.utils.localization import TranslateFn, localize_message

from .utils.file_types import FILE_TYPE_LABELS

REPORT_BASE = "modules.nsfw_scanner.helpers.attachments.report"
SHARED_BOOLEAN = "modules.nsfw_scanner.shared.boolean"
SHARED_CATEGORY = "modules.nsfw_scanner.shared.category"
SHARED_ROOT = "modules.nsfw_scanner.shared"
NSFW_CATEGORY_NAMESPACE = "cogs.nsfw.meta.categories"

DECISION_FALLBACKS = {
    "unknown": "Unknown",
    "nsfw": "NSFW",
    "safe": "Safe",
}

FIELD_FALLBACKS = {
    "reason": "Reason",
    "category": "Category",
    "score": "Score",
    "flagged_any": "Flagged Any",
    "summary_categories": "Summary Categories",
    "max_similarity": "Max Similarity",
    "max_category": "Max Similarity Category",
    "similarity": "Matched Similarity",
    "high_accuracy": "High Accuracy",
    "clip_threshold": "CLIP Threshold",
    "moderation_threshold": "Moderation Threshold",
    "video_frames": "Video Frames",
    "latency_ms": "Scan Latency",
    "average_latency_per_frame_ms": "Avg Latency / Frame",
    "latency_breakdown": "Latency Breakdown",
    "cache_status": "Cache Status",
}

REASON_FALLBACKS = {
    "openai_moderation": "OpenAI moderation",
    "similarity_match": "Similarity match",
    "no_frames_extracted": "No frames extracted",
    "no_nsfw_frames_detected": "No NSFW frames detected",
}


def resolve_translator(scanner) -> TranslateFn | None:
    translate = getattr(getattr(scanner, "bot", None), "translate", None)
    return translate if callable(translate) else None


def _localize_decision(translator: TranslateFn | None, decision: str, guild_id: int | None) -> str:
    fallback = DECISION_FALLBACKS.get(decision, decision.capitalize())
    return localize_message(
        translator,
        REPORT_BASE,
        f"decision.{decision}",
        fallback=fallback,
        guild_id=guild_id,
    )


def _localize_field_name(translator: TranslateFn | None, field: str, guild_id: int | None) -> str:
    fallback = FIELD_FALLBACKS.get(field, field.replace("_", " ").title())
    return localize_message(
        translator,
        REPORT_BASE,
        f"fields.{field}",
        fallback=fallback,
        guild_id=guild_id,
    )


def _localize_reason(translator: TranslateFn | None, reason: Any, guild_id: int | None) -> str | None:
    if reason is None:
        return None
    if isinstance(reason, str):
        normalized = reason if reason in REASON_FALLBACKS else reason.lower().replace(" ", "_")
        fallback = REASON_FALLBACKS.get(normalized, str(reason))
        return localize_message(
            translator,
            REPORT_BASE,
            f"reasons.{normalized}",
            fallback=fallback,
            guild_id=guild_id,
        )
    return str(reason)


def _localize_boolean(translator: TranslateFn | None, value: bool, guild_id: int | None) -> str:
    key = "true" if value else "false"
    return localize_message(
        translator,
        SHARED_BOOLEAN,
        key,
        fallback=key,
        guild_id=guild_id,
    )


def _localize_category(translator: TranslateFn | None, category: str | None, guild_id: int | None) -> str:
    if not category:
        return localize_message(
            translator,
            SHARED_CATEGORY,
            "unspecified",
            fallback="Unspecified",
            guild_id=guild_id,
        )
    normalized = category.lower().replace(" ", "_")
    return localize_message(
        translator,
        NSFW_CATEGORY_NAMESPACE,
        normalized,
        fallback=category.replace("_", " ").title(),
        guild_id=guild_id,
    )


async def emit_verbose_report(
    scanner,
    *,
    message: discord.Message | None,
    author,
    guild_id: int | None,
    file_type: str | None,
    detected_mime: str | None,
    scan_result: dict[str, Any] | None,
    duration_ms: int,
) -> None:
    if message is None or guild_id is None or scan_result is None:
        return

    translator = resolve_translator(scanner)
    decision_key = "unknown"
    if scan_result.get("is_nsfw") is True:
        decision_key = "nsfw"
    elif scan_result.get("is_nsfw") is False:
        decision_key = "safe"

    decision_label = _localize_decision(translator, decision_key, guild_id)
    normalized_file_type = (file_type or "unknown").lower()
    file_type_label = localize_message(
        translator,
        REPORT_BASE,
        f"file_types.{normalized_file_type}",
        fallback=FILE_TYPE_LABELS.get(normalized_file_type, detected_mime or normalized_file_type.title()),
        guild_id=guild_id,
    )

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
                    fallback="**User:** {user}",
                    guild_id=guild_id,
                ),
                localize_message(
                    translator,
                    REPORT_BASE,
                    "description.type",
                    placeholders={"file_type": file_type_label},
                    fallback="**Type:** {file_type}",
                    guild_id=guild_id,
                ),
                localize_message(
                    translator,
                    REPORT_BASE,
                    "description.decision",
                    placeholders={"decision": decision_label},
                    fallback="**Decision:** {decision}",
                    guild_id=guild_id,
                ),
            ]
        ),
        color=discord.Color.orange() if scan_result.get("is_nsfw") else discord.Color.green(),
    )
    embed.add_field(
        name=_localize_field_name(translator, "latency_ms", guild_id),
        value=f"{duration_ms} ms",
        inline=True,
    )

    cache_status = scan_result.get("cache_status")
    if cache_status:
        embed.add_field(
            name=_localize_field_name(translator, "cache_status", guild_id),
            value=str(cache_status),
            inline=True,
        )

    for field_key, fallback_key in ("reason", "score"), ("category", "category"):
        value = scan_result.get(field_key)
        if value is None:
            continue
        if field_key == "reason":
            value = _localize_reason(translator, value, guild_id)
        elif field_key == "category":
            value = _localize_category(translator, value, guild_id)
        embed.add_field(
            name=_localize_field_name(translator, fallback_key, guild_id),
            value=value,
            inline=True,
        )

    pipeline_metrics = scan_result.get("pipeline_metrics")
    if isinstance(pipeline_metrics, dict):
        latency_breakdown = pipeline_metrics.get("latency_breakdown_ms")
        if isinstance(latency_breakdown, dict):
            lines: list[str] = []
            for key, entry in latency_breakdown.items():
                duration = entry.get("duration_ms") if isinstance(entry, dict) else entry
                if duration is None:
                    continue
                try:
                    duration = float(duration)
                except (TypeError, ValueError):
                    continue
                label = entry.get("label") if isinstance(entry, dict) else key.replace("_", " ").title()
                lines.append(f"{label}: {duration:.1f} ms")
            if lines:
                embed.add_field(
                    name=_localize_field_name(translator, "latency_breakdown", guild_id),
                    value="\n".join(lines),
                    inline=False,
                )

    avatar = getattr(getattr(actor, "display_avatar", None), "url", None)
    if avatar:
        embed.set_thumbnail(url=avatar)

    try:
        await mod_logging.log_to_channel(
            embed=embed,
            channel_id=message.channel.id,
            bot=scanner.bot,
        )
    except Exception as exc:
        print(f"[verbose] Failed to send verbose embed: {exc}")


async def dispatch_callback(
    *,
    scanner,
    nsfw_callback,
    author,
    guild_id: int,
    scan_result: dict[str, Any],
    message: discord.Message,
    file: Optional[discord.File],
) -> None:
    translator = resolve_translator(scanner)
    category_name = scan_result.get("category") or "unspecified"
    confidence_value = None
    confidence_source = None
    try:
        if scan_result.get("score") is not None:
            confidence_value = float(scan_result.get("score"))
            confidence_source = "score"
        elif scan_result.get("similarity") is not None:
            confidence_value = float(scan_result.get("similarity"))
            confidence_source = "similarity"
    except Exception:
        confidence_value = None
        confidence_source = None

    category_label = _localize_category(translator, category_name, guild_id)
    reason = localize_message(
        translator,
        SHARED_ROOT,
        "policy_violation",
        placeholders={"category": category_label},
        fallback="Detected potential policy violation (Category: **{category}**)",
        guild_id=guild_id,
    )

    evidence_file = file
    if evidence_file is None:
        return
    if not nsfw_callback or not scan_result:
        try:
            evidence_file.close()
        except Exception:
            pass
        file_obj = getattr(evidence_file, "fp", None)
        if file_obj:
            try:
                file_obj.close()
            except Exception:
                pass
        return

    try:
        await nsfw_callback(
            author,
            scanner.bot,
            guild_id,
            reason,
            evidence_file,
            message,
            confidence=confidence_value,
            confidence_source=confidence_source,
        )
    finally:
        try:
            evidence_file.close()
        except Exception:
            pass
        file_obj = getattr(evidence_file, "fp", None)
        if file_obj:
            try:
                file_obj.close()
            except Exception:
                pass


__all__ = [
    "emit_verbose_report",
    "dispatch_callback",
    "resolve_translator",
]
