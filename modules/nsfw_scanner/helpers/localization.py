"""Shared localization helpers for NSFW scanner embeds and reports."""

from __future__ import annotations

from typing import Any

from modules.utils.localization import TranslateFn, localize_message


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


def localize_decision(
    translator: TranslateFn | None, decision: str, guild_id: int | None
) -> str:
    fallback = DECISION_FALLBACKS.get(decision, decision.capitalize())
    return localize_message(
        translator,
        REPORT_BASE,
        f"decision.{decision}",
        fallback=fallback,
        guild_id=guild_id,
    )


def localize_field_name(
    translator: TranslateFn | None, field: str, guild_id: int | None
) -> str:
    fallback = FIELD_FALLBACKS.get(field, field.replace("_", " ").title())
    return localize_message(
        translator,
        REPORT_BASE,
        f"fields.{field}",
        fallback=fallback,
        guild_id=guild_id,
    )


def localize_reason(
    translator: TranslateFn | None, reason: Any, guild_id: int | None
) -> str | None:
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


def localize_boolean(
    translator: TranslateFn | None, value: bool, guild_id: int | None
) -> str:
    key = "true" if value else "false"
    return localize_message(
        translator,
        SHARED_BOOLEAN,
        key,
        fallback=key,
        guild_id=guild_id,
    )


def localize_category(
    translator: TranslateFn | None, category: str | None, guild_id: int | None
) -> str:
    source = (category or "").strip()
    if not source:
        return localize_message(
            translator,
            SHARED_CATEGORY,
            "unspecified",
            fallback="Unspecified",
            guild_id=guild_id,
        )

    normalized = (
        source.replace("/", "_")
        .replace("-", "_")
        .replace(" ", "_")
        .lower()
    )
    if not normalized:
        normalized = "unspecified"

    namespace = SHARED_CATEGORY if normalized == "unspecified" else NSFW_CATEGORY_NAMESPACE
    fallback = normalized.replace("_", " ").title()
    return localize_message(
        translator,
        namespace,
        normalized,
        fallback=fallback,
        guild_id=guild_id,
    )

