import os
import time
from typing import Any

import discord

from modules.metrics import log_media_scan
from modules.utils import mod_logging, mysql
from modules.utils.localization import TranslateFn, localize_message

from ..utils.file_types import (
    FILE_TYPE_IMAGE,
    FILE_TYPE_LABELS,
    FILE_TYPE_VIDEO,
    determine_file_type,
)
from ..logging_utils import log_slow_scan_if_needed
from .images import build_image_processing_context, process_image
from .metrics import LatencyTracker, collect_scan_telemetry, format_video_scan_progress
from .videos import process_video

REPORT_BASE = "modules.nsfw_scanner.helpers.attachments.report"
SHARED_BOOLEAN = "modules.nsfw_scanner.shared.boolean"
SHARED_CATEGORY = "modules.nsfw_scanner.shared.category"
SHARED_ROOT = "modules.nsfw_scanner.shared"
NSFW_CATEGORY_NAMESPACE = "cogs.nsfw.meta.categories"

_CACHE_MISS = object()


class AttachmentSettingsCache:
    """Cache frequently accessed guild settings for a scan batch."""

    __slots__ = (
        "scan_settings",
        "nsfw_verbose",
        "check_tenor_gifs",
        "premium_status",
        "premium_plan",
        "text_enabled",
        "accelerated",
    )

    def __init__(self) -> None:
        self.scan_settings: Any = _CACHE_MISS
        self.nsfw_verbose: Any = _CACHE_MISS
        self.check_tenor_gifs: Any = _CACHE_MISS
        self.premium_status: Any = _CACHE_MISS
        self.premium_plan: Any = _CACHE_MISS
        self.text_enabled: Any = _CACHE_MISS
        self.accelerated: Any = _CACHE_MISS

    def has_scan_settings(self) -> bool:
        return self.scan_settings is not _CACHE_MISS

    def get_scan_settings(self) -> dict[str, Any] | None:
        if self.scan_settings is _CACHE_MISS:
            return None
        return self.scan_settings or {}

    def set_scan_settings(self, value: dict[str, Any] | None) -> None:
        self.scan_settings = value or {}

    def has_verbose(self) -> bool:
        return self.nsfw_verbose is not _CACHE_MISS

    def get_verbose(self) -> bool | None:
        if self.nsfw_verbose is _CACHE_MISS:
            return None
        return bool(self.nsfw_verbose)

    def set_verbose(self, value: bool | None) -> None:
        self.nsfw_verbose = bool(value)

    def has_check_tenor(self) -> bool:
        return self.check_tenor_gifs is not _CACHE_MISS

    def get_check_tenor(self) -> bool | None:
        if self.check_tenor_gifs is _CACHE_MISS:
            return None
        return bool(self.check_tenor_gifs)

    def set_check_tenor(self, value: bool | None) -> None:
        self.check_tenor_gifs = bool(value)

    def has_premium_status(self) -> bool:
        return self.premium_status is not _CACHE_MISS

    def get_premium_status(self) -> Any:
        if self.premium_status is _CACHE_MISS:
            return None
        return self.premium_status

    def set_premium_status(self, value: Any) -> None:
        self.premium_status = value if value is not None else {}

    def has_premium_plan(self) -> bool:
        return self.premium_plan is not _CACHE_MISS

    def get_premium_plan(self) -> Any:
        if self.premium_plan is _CACHE_MISS:
            return None
        return self.premium_plan

    def set_premium_plan(self, value: Any) -> None:
        self.premium_plan = value

    def has_text_enabled(self) -> bool:
        return self.text_enabled is not _CACHE_MISS

    def get_text_enabled(self) -> bool | None:
        if self.text_enabled is _CACHE_MISS:
            return None
        return bool(self.text_enabled)

    def set_text_enabled(self, value: Any) -> None:
        self.text_enabled = bool(value)

    def has_accelerated(self) -> bool:
        return self.accelerated is not _CACHE_MISS

    def get_accelerated(self) -> bool | None:
        if self.accelerated is _CACHE_MISS:
            return None
        return bool(self.accelerated)

    def set_accelerated(self, value: Any) -> None:
        self.accelerated = bool(value)

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
    settings_cache: AttachmentSettingsCache | None = None,
    *,
    pre_latency_steps: dict[str, dict[str, Any]] | None = None,
    pre_download_bytes: int | None = None,
    source_url: str | None = None,
    overall_started_at: float | None = None,
) -> bool:
    if settings_cache is None:
        settings_cache = AttachmentSettingsCache()

    latency_tracker = LatencyTracker(
        started_at=overall_started_at,
        steps=pre_latency_steps,
    )

    filename = os.path.basename(temp_filename)
    mime_started = time.perf_counter()
    file_type, detected_mime = determine_file_type(temp_filename)
    latency_tracker.record_step(
        "attachment_mime_detection",
        (time.perf_counter() - mime_started) * 1000,
        label="MIME Detection",
    )
    try:
        size_started = time.perf_counter()
        file_size = os.path.getsize(temp_filename)
        latency_tracker.record_step(
            "attachment_filesize",
            (time.perf_counter() - size_started) * 1000,
            label="File Size Lookup",
        )
    except OSError:
        file_size = None

    metrics_recorded = False
    accelerated_cache: dict[str, Any] = {"fetched": False, "value": None}
    pipeline_accelerated: bool | None = None

    async def _get_accelerated() -> bool | None:
        nonlocal pipeline_accelerated
        if pipeline_accelerated is not None:
            return pipeline_accelerated
        if accelerated_cache["fetched"]:
            return accelerated_cache["value"]
        accelerated_cache["fetched"] = True
        if guild_id is None:
            accelerated_cache["value"] = None
            return None
        try:
            lookup_started = time.perf_counter()
            accelerated_cache["value"] = await mysql.is_accelerated(
                guild_id=guild_id
            )
            latency_tracker.record_step(
                "attachment_accelerated_lookup",
                (time.perf_counter() - lookup_started) * 1000,
                label="Accelerated Lookup",
            )
        except Exception:
            accelerated_cache["value"] = None
        value = accelerated_cache["value"]
        if isinstance(value, bool):
            pipeline_accelerated = value
        return accelerated_cache["value"]

    async def _emit_metrics(
        result: dict[str, Any] | None,
        status: str,
        *,
        duration_override_ms: int | None = None,
    ) -> None:
        nonlocal metrics_recorded, pipeline_accelerated
        if metrics_recorded:
            return
        metrics_recorded = True

        if duration_override_ms is not None:
            duration_ms = int(round(duration_override_ms))
        else:
            duration_ms = int(round(latency_tracker.total_duration_ms()))
        channel_id = getattr(getattr(message, "channel", None), "id", None) if message else None
        user_id = getattr(author, "id", None) if author else None
        message_id = getattr(message, "id", None) if message else None

        extra_context = {
            "status": status,
            "detected_mime": detected_mime,
            "file_type": file_type,
            "perform_actions": bool(perform_actions),
            "nsfw_callback": bool(nsfw_callback),
        }
        if message_id:
            extra_context["message_id"] = message_id
        if message and getattr(message, "jump_url", None):
            extra_context["jump_url"] = message.jump_url

        try:
            accelerated_flag = pipeline_accelerated
            if accelerated_flag is None:
                accelerated_flag = await _get_accelerated()
            await log_media_scan(
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=user_id,
                message_id=message_id,
                content_type=file_type or "unknown",
                detected_mime=detected_mime,
                filename=filename,
                file_size=file_size,
                source="attachment",
                scan_result=result,
                status=status,
                scan_duration_ms=duration_ms,
                accelerated=accelerated_flag,
                reference=f"{message_id}:{filename}" if message_id else filename,
                extra_context=extra_context,
            )
        except Exception as metrics_exc:  # pragma: no cover - best effort logging
            print(f"[metrics] Failed to record media scan metric for {filename}: {metrics_exc}")

    if guild_id is None:
        await _emit_metrics(
            None,
            "missing_guild",
            duration_override_ms=latency_tracker.total_duration_ms(),
        )
        print("[check_attachment] Guild_id is None")
        return False

    file = None
    scan_result: dict[str, Any] | None = None

    settings = settings_cache.get_scan_settings()
    if settings is None and guild_id is not None:
        settings_started = time.perf_counter()
        try:
            settings = await mysql.get_settings(guild_id)
        except Exception:
            settings = {}
        latency_tracker.record_step(
            "attachment_settings_lookup",
            (time.perf_counter() - settings_started) * 1000,
            label="Settings Lookup",
        )
        settings_cache.set_scan_settings(settings)
        settings = settings_cache.get_scan_settings()
    settings = settings or {}

    accelerated_value = await _get_accelerated()
    context_started = time.perf_counter()
    context = await build_image_processing_context(
        guild_id,
        settings=settings,
        accelerated=accelerated_value,
    )
    latency_tracker.record_step(
        "attachment_context_build",
        (time.perf_counter() - context_started) * 1000,
        label="Build Scan Context",
    )
    pipeline_accelerated = bool(context.accelerated)

    payload_metadata: dict[str, Any] | None = {
        "guild_id": guild_id,
        "channel_id": getattr(getattr(message, "channel", None), "id", None)
        if message
        else None,
        "message_id": getattr(message, "id", None) if message else None,
        "author_id": getattr(author, "id", None) if author else None,
        "user_id": getattr(author, "id", None) if author else None,
        "message_jump_url": getattr(message, "jump_url", None) if message else None,
        "source_url": source_url,
    }
    payload_metadata = {
        key: value for key, value in (payload_metadata or {}).items() if value is not None
    }

    if file_type == FILE_TYPE_IMAGE:
        scan_result = await process_image(
            scanner,
            original_filename=temp_filename,
            guild_id=guild_id,
            clean_up=False,
            settings=settings,
            accelerated=accelerated_value,
            context=context,
            source_url=source_url,
            payload_metadata=payload_metadata,
        )
    elif file_type == FILE_TYPE_VIDEO:
        premium_status = None
        if guild_id is not None:
            if settings_cache.has_premium_status():
                premium_status = settings_cache.get_premium_status()
            else:
                premium_started = time.perf_counter()
                premium_status = await mysql.get_premium_status(guild_id)
                latency_tracker.record_step(
                    "attachment_premium_lookup",
                    (time.perf_counter() - premium_started) * 1000,
                    label="Premium Lookup",
                )
                settings_cache.set_premium_status(premium_status)
                premium_status = settings_cache.get_premium_status()
        file, scan_result = await process_video(
            scanner,
            original_filename=temp_filename,
            guild_id=guild_id,
            context=context,
            premium_status=premium_status,
            payload_metadata=payload_metadata,
        )
    else:
        await _emit_metrics(
            None,
            "unsupported_type",
            duration_override_ms=latency_tracker.total_duration_ms(),
        )
        return False

    translator = _resolve_translator(scanner)

    resolved_total_latency = latency_tracker.total_duration_ms()

    if isinstance(scan_result, dict):
        pipeline_metrics = scan_result.setdefault("pipeline_metrics", {})
        pipeline_metrics, resolved_total_latency = latency_tracker.merge_into_pipeline(
            pipeline_metrics
        )
        scan_result["pipeline_metrics"] = pipeline_metrics
        if pre_download_bytes is not None and pipeline_metrics.get("bytes_downloaded") is None:
            pipeline_metrics["bytes_downloaded"] = pre_download_bytes
        elif pipeline_metrics.get("bytes_downloaded") is None and file_size is not None:
            pipeline_metrics["bytes_downloaded"] = file_size

    scan_duration_ms = int(round(resolved_total_latency))

    telemetry = collect_scan_telemetry(scan_result)
    telemetry_total_latency = telemetry.total_latency_ms
    if telemetry_total_latency is None:
        resolved_total_latency_ms = scan_duration_ms
    else:
        try:
            resolved_total_latency_ms = int(round(float(telemetry_total_latency)))
        except (TypeError, ValueError):
            resolved_total_latency_ms = scan_duration_ms

    try:
        await log_slow_scan_if_needed(
            bot=scanner.bot,
            scan_result=scan_result,
            media_type=file_type,
            detected_mime=detected_mime,
            total_duration_ms=resolved_total_latency_ms,
            filename=filename,
            message=message,
            telemetry=telemetry,
        )
    except Exception as exc:
        print(f"[slow-log] Failed to emit slow scan log: {exc}")

    try:
        verbose_enabled = False
        if message is not None and guild_id is not None:
            if settings_cache.has_verbose():
                verbose_enabled = bool(settings_cache.get_verbose())
            else:
                verbose_enabled = bool(await mysql.get_settings(guild_id, "nsfw-verbose"))
                settings_cache.set_verbose(verbose_enabled)
        if message is not None and verbose_enabled:
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

            duration_display_ms = resolved_total_latency_ms
            embed.add_field(
                name=_localize_field_name(translator, "latency_ms", guild_id),
                value=f"{duration_display_ms} ms",
                inline=True,
            )
            if telemetry.breakdown_lines:
                embed.add_field(
                    name=_localize_field_name(
                        translator, "latency_breakdown", guild_id
                    ),
                    value="\n".join(telemetry.breakdown_lines)[:1024],
                    inline=False,
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
                frame_metrics = telemetry.frame_metrics
                if frame_metrics.scanned is not None:
                    progress_value = format_video_scan_progress(frame_metrics)
                    embed.add_field(
                        name=_localize_field_name(
                            translator, "video_frames", guild_id
                        ),
                        value=str(progress_value)[:1024] if progress_value else "?",
                        inline=True,
                    )
                    if telemetry.average_latency_per_frame_ms is not None:
                        embed.add_field(
                            name=_localize_field_name(
                                translator, "average_latency_per_frame_ms", guild_id
                            ),
                            value=f"{telemetry.average_latency_per_frame_ms:.2f} ms/frame",
                            inline=True,
                        )

            avatar_url = None
            if actor is not None:
                avatar = getattr(actor, "display_avatar", None)
                if avatar:
                    avatar_url = avatar.url
            if avatar_url:
                embed.set_thumbnail(url=avatar_url)
            await mod_logging.log_to_channel(
                embed=embed,
                channel_id=message.channel.id,
                bot=scanner.bot,
            )
    except Exception as exc:
        print(f"[verbose] Failed to send verbose embed: {exc}")

    await _emit_metrics(
        scan_result,
        "scan_complete",
        duration_override_ms=resolved_total_latency_ms,
    )

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
