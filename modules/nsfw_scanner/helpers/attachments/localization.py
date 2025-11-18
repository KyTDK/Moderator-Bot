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
    "payload_details": "Payload Details",
}

REASON_FALLBACKS = {
    "openai_moderation": "OpenAI moderation",
    "openai_moderation_timeout": "OpenAI moderation timeout",
    "openai_moderation_http_timeout": "OpenAI moderation HTTP timeout",
    "openai_moderation_connection_error": "OpenAI moderation connection error",
    "similarity_match": "Similarity match",
    "no_frames_extracted": "No frames extracted",
    "no_nsfw_frames_detected": "No NSFW frames detected",
}

__all__ = [
    "REPORT_BASE",
    "SHARED_ROOT",
    "resolve_translator",
    "localize_decision",
    "localize_field_name",
    "localize_reason",
    "localize_boolean",
    "localize_category",
]


def resolve_translator(scanner) -> TranslateFn | None:
    bot = getattr(scanner, "bot", None)
    translate = getattr(bot, "translate", None) if bot else None
    return translate if callable(translate) else None


def localize_decision(translator: TranslateFn | None, decision: str, guild_id: int | None) -> str:
    normalized = (decision or "unknown").strip().lower()
    fallback = DECISION_FALLBACKS.get(normalized, normalized.capitalize())
    return localize_message(
        translator,
        REPORT_BASE,
        f"decision.{normalized}",
        fallback=fallback,
        guild_id=guild_id,
    )


def localize_field_name(translator: TranslateFn | None, field: str, guild_id: int | None) -> str:
    return localize_message(
        translator,
        REPORT_BASE,
        f"fields.{field}",
        fallback=FIELD_FALLBACKS.get(field, field.replace("_", " ").title()),
        guild_id=guild_id,
    )


def localize_reason(translator: TranslateFn | None, value: Any, guild_id: int | None) -> str | None:
    if not value:
        return None
    if isinstance(value, str):
        normalized = value if value in REASON_FALLBACKS else value.lower().replace(" ", "_")
        fallback = REASON_FALLBACKS.get(normalized, str(value))
    else:
        normalized = str(value)
        fallback = normalized
    return localize_message(
        translator,
        REPORT_BASE,
        f"reasons.{normalized}",
        fallback=fallback,
        guild_id=guild_id,
    )


def localize_boolean(translator: TranslateFn | None, value: bool, guild_id: int | None) -> str:
    key = "true" if value else "false"
    return localize_message(
        translator,
        SHARED_BOOLEAN,
        key,
        fallback=key,
        guild_id=guild_id,
    )


def localize_category(translator: TranslateFn | None, category: str, guild_id: int | None) -> str:
    normalized = (category or "unspecified").strip()
    if not normalized:
        normalized = "unspecified"
    normalized = normalized.replace("/", "_").replace("-", "_").lower()
    fallback = normalized.replace("_", " ").title()
    if translator is None:
        return fallback
    namespace = SHARED_CATEGORY if normalized == "unspecified" else NSFW_CATEGORY_NAMESPACE
    return localize_message(
        translator,
        namespace,
        normalized,
        fallback=fallback,
        guild_id=guild_id,
    )
