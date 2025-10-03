import os
from typing import Any

import discord

from modules.utils import mod_logging, mysql
from modules.utils.localization import TranslateFn, localize_message

from ..utils import (
    determine_file_type,
    FILE_TYPE_IMAGE,
    FILE_TYPE_LABELS,
    FILE_TYPE_VIDEO,
)
from .images import process_image
from .videos import process_video

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
}

REASON_FALLBACKS = {
    "openai_moderation": "OpenAI moderation",
    "similarity_match": "Similarity match",
    "no_frames_extracted": "No frames extracted",
    "no_nsfw_frames_detected": "No NSFW frames detected",
}


def _resolve_translator(scanner) -> TranslateFn | None:
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


def _localize_reason(
    translator: TranslateFn | None,
    reason: Any,
    guild_id: int | None,
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


def _localize_boolean(
    translator: TranslateFn | None,
    value: bool,
    guild_id: int | None,
) -> str:
    key = "true" if value else "false"
    return localize_message(
        translator,
        SHARED_BOOLEAN,
        key,
        fallback=key,
        guild_id=guild_id,
    )


def _localize_category(
    translator: TranslateFn | None,
    category: str | None,
    guild_id: int | None,
) -> str:
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


async def check_attachment(
    scanner,
    author,
    temp_filename: str,
    nsfw_callback,
    guild_id: int | None,
    message,
    perform_actions: bool = True,
) -> bool:
    filename = os.path.basename(temp_filename)
    file_type, detected_mime = determine_file_type(temp_filename)

    if guild_id is None:
        print("[check_attachment] Guild_id is None")
        return False

    file = None
    scan_result: dict[str, Any] | None = None

    if file_type == FILE_TYPE_IMAGE:
        scan_result = await process_image(
            scanner,
            original_filename=temp_filename,
            guild_id=guild_id,
            clean_up=False,
        )
    elif file_type == FILE_TYPE_VIDEO:
        file, scan_result = await process_video(
            scanner,
            original_filename=temp_filename,
            guild_id=guild_id,
        )
    else:
        print(
            f"[check_attachment] Unsupported file type: {detected_mime or file_type} for {filename}"
        )
        return False

    translator = _resolve_translator(scanner)

    try:
        if message is not None and await mysql.get_settings(guild_id, "nsfw-verbose"):
            decision_key = "unknown"
            if scan_result is not None:
                decision_key = "nsfw" if scan_result.get("is_nsfw") else "safe"
            decision_label = _localize_decision(translator, decision_key, guild_id)
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
                            placeholders={"user": author.mention},
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
            if scan_result:
                reason_value = _localize_reason(
                    translator, scan_result.get("reason"), guild_id
                )
                if reason_value:
                    embed.add_field(
                        name=_localize_field_name(translator, "reason", guild_id),
                        value=str(reason_value)[:1024],
                        inline=False,
                    )
                if scan_result.get("category"):
                    embed.add_field(
                        name=_localize_field_name(translator, "category", guild_id),
                        value=_localize_category(
                            translator,
                            str(scan_result.get("category")),
                            guild_id,
                        ),
                        inline=True,
                    )
                if scan_result.get("score") is not None:
                    embed.add_field(
                        name=_localize_field_name(translator, "score", guild_id),
                        value=f"{float(scan_result.get('score') or 0):.3f}",
                        inline=True,
                    )
                if scan_result.get("flagged_any") is not None:
                    embed.add_field(
                        name=_localize_field_name(
                            translator, "flagged_any", guild_id
                        ),
                        value=_localize_boolean(
                            translator,
                            bool(scan_result.get("flagged_any")),
                            guild_id,
                        ),
                        inline=True,
                    )
                if scan_result.get("summary_categories") is not None:
                    embed.add_field(
                        name=_localize_field_name(
                            translator, "summary_categories", guild_id
                        ),
                        value=str(scan_result.get("summary_categories")),
                        inline=False,
                    )
                if scan_result.get("max_similarity") is not None:
                    embed.add_field(
                        name=_localize_field_name(
                            translator, "max_similarity", guild_id
                        ),
                        value=f"{float(scan_result.get('max_similarity') or 0):.3f}",
                        inline=True,
                    )
                if scan_result.get("max_category") is not None:
                    embed.add_field(
                        name=_localize_field_name(
                            translator, "max_category", guild_id
                        ),
                        value=str(scan_result.get("max_category")),
                        inline=True,
                    )
                if scan_result.get("similarity") is not None:
                    embed.add_field(
                        name=_localize_field_name(
                            translator, "similarity", guild_id
                        ),
                        value=f"{float(scan_result.get('similarity') or 0):.3f}",
                        inline=True,
                    )
                if scan_result.get("high_accuracy") is not None:
                    embed.add_field(
                        name=_localize_field_name(
                            translator, "high_accuracy", guild_id
                        ),
                        value=_localize_boolean(
                            translator,
                            bool(scan_result.get("high_accuracy")),
                            guild_id,
                        ),
                        inline=True,
                    )
                if scan_result.get("clip_threshold") is not None:
                    embed.add_field(
                        name=_localize_field_name(
                            translator, "clip_threshold", guild_id
                        ),
                        value=f"{float(scan_result.get('clip_threshold') or 0):.3f}",
                        inline=True,
                    )
                if scan_result.get("threshold") is not None:
                    try:
                        embed.add_field(
                            name=_localize_field_name(
                                translator, "moderation_threshold", guild_id
                            ),
                            value=f"{float(scan_result.get('threshold') or 0):.3f}",
                            inline=True,
                        )
                    except Exception:
                        pass
                if scan_result.get("video_frames_scanned") is not None:
                    scanned = scan_result.get("video_frames_scanned")
                    target = scan_result.get("video_frames_target")
                    embed.add_field(
                        name=_localize_field_name(
                            translator, "video_frames", guild_id
                        ),
                        value=f"{scanned}/{target}",
                        inline=True,
                    )

            embed.set_thumbnail(url=author.display_avatar.url)
            await mod_logging.log_to_channel(
                embed=embed,
                channel_id=message.channel.id,
                bot=scanner.bot,
            )
    except Exception as exc:
        print(f"[verbose] Failed to send verbose embed: {exc}")

    if not perform_actions:
        return False

    if nsfw_callback and scan_result and scan_result.get("is_nsfw"):
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

        if file is None:
            file = discord.File(temp_filename, filename=filename)
        try:
            category_label = _localize_category(
                translator,
                category_name,
                guild_id,
            )
            await nsfw_callback(
                author,
                scanner.bot,
                guild_id,
                localize_message(
                    translator,
                    SHARED_ROOT,
                    "policy_violation",
                    placeholders={"category": category_label},
                    fallback="Detected potential policy violation (Category: **{category}**)",
                    guild_id=guild_id,
                ),
                file,
                message,
                confidence=confidence_value,
                confidence_source=confidence_source,
            )
        finally:
            try:
                file.close()
            except Exception:
                try:
                    file.fp.close()
                except Exception:
                    pass
        return True

    return False
