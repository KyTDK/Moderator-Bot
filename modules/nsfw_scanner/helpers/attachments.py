import os
import time
from typing import Any

import discord

from cogs.nsfw import NSFW_CATEGORY_SETTING
from modules.metrics import log_media_scan
from modules.utils import mysql
from modules.utils.localization import localize_message

from ..utils.file_types import (
    FILE_TYPE_IMAGE,
    FILE_TYPE_LABELS,
    FILE_TYPE_VIDEO,
    determine_file_type,
)
from ..reporting import ScanFieldSpec, emit_verbose_report
from .images import build_image_processing_context, process_image
from .videos import process_video

from .localization import (
    SHARED_ROOT,
    localize_boolean,
    localize_category,
    localize_reason,
    resolve_translator,
)

_CACHE_MISS = object()


class AttachmentSettingsCache:
    """Cache frequently accessed guild settings for a scan batch."""

    __slots__ = (
        "scan_settings",
        "nsfw_verbose",
        "check_tenor_gifs",
        "premium_status",
        "premium_plan",
    )

    def __init__(self) -> None:
        self.scan_settings: Any = _CACHE_MISS
        self.nsfw_verbose: Any = _CACHE_MISS
        self.check_tenor_gifs: Any = _CACHE_MISS
        self.premium_status: Any = _CACHE_MISS
        self.premium_plan: Any = _CACHE_MISS

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




async def check_attachment(
    scanner,
    author,
    temp_filename: str,
    nsfw_callback,
    guild_id: int | None,
    message,
    perform_actions: bool = True,
    settings_cache: AttachmentSettingsCache | None = None,
) -> bool:
    if settings_cache is None:
        settings_cache = AttachmentSettingsCache()

    filename = os.path.basename(temp_filename)
    file_type, detected_mime = determine_file_type(temp_filename)
    try:
        file_size = os.path.getsize(temp_filename)
    except OSError:
        file_size = None

    started_at = time.perf_counter()
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
            accelerated_cache["value"] = await mysql.is_accelerated(guild_id=guild_id)
        except Exception:
            accelerated_cache["value"] = None
        value = accelerated_cache["value"]
        if isinstance(value, bool):
            pipeline_accelerated = value
        return accelerated_cache["value"]

    async def _emit_metrics(result: dict[str, Any] | None, status: str) -> None:
        nonlocal metrics_recorded, pipeline_accelerated
        if metrics_recorded:
            return
        metrics_recorded = True

        duration_ms = int(max((time.perf_counter() - started_at) * 1000, 0))
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
        await _emit_metrics(None, "missing_guild")
        print("[check_attachment] Guild_id is None")
        return False

    file = None
    scan_result: dict[str, Any] | None = None

    settings = settings_cache.get_scan_settings()
    if settings is None and guild_id is not None:
        settings = await mysql.get_settings(
            guild_id,
            [NSFW_CATEGORY_SETTING, "threshold", "nsfw-high-accuracy"],
        )
        settings_cache.set_scan_settings(settings)
        settings = settings_cache.get_scan_settings()
    settings = settings or {}

    accelerated_value = await _get_accelerated()
    context = await build_image_processing_context(
        guild_id,
        settings=settings,
        accelerated=accelerated_value,
    )
    pipeline_accelerated = bool(context.accelerated)

    if file_type == FILE_TYPE_IMAGE:
        scan_result = await process_image(
            scanner,
            original_filename=temp_filename,
            guild_id=guild_id,
            clean_up=False,
            settings=settings,
            accelerated=accelerated_value,
            context=context,
        )
    elif file_type == FILE_TYPE_VIDEO:
        premium_status = None
        if guild_id is not None:
            if settings_cache.has_premium_status():
                premium_status = settings_cache.get_premium_status()
            else:
                premium_status = await mysql.get_premium_status(guild_id)
                settings_cache.set_premium_status(premium_status)
                premium_status = settings_cache.get_premium_status()
        file, scan_result = await process_video(
            scanner,
            original_filename=temp_filename,
            guild_id=guild_id,
            context=context,
            premium_status=premium_status,
        )
    else:
        await _emit_metrics(None, "unsupported_type")
        print(
            f"[check_attachment] Unsupported file type: {detected_mime or file_type} for {filename}"
        )
        return False

    translator = resolve_translator(scanner)
    await _emit_metrics(scan_result, "scan_complete")

    try:
        verbose_enabled = False
        if message is not None and guild_id is not None:
            if settings_cache.has_verbose():
                verbose_enabled = bool(settings_cache.get_verbose())
            else:
                verbose_enabled = bool(await mysql.get_settings(guild_id, "nsfw-verbose"))
                settings_cache.set_verbose(verbose_enabled)
        if message is not None and verbose_enabled:
            duration_ms = int(max((time.perf_counter() - started_at) * 1000, 0))

            def _format_reason(value, _scan_result, translator, guild_id, _duration_ms):
                if value is None:
                    return None
                return localize_reason(translator, value, guild_id)

            def _format_category(value, _scan_result, translator, guild_id, _duration_ms):
                if value is None:
                    return None
                return localize_category(translator, value, guild_id)

            def _format_float(value, _scan_result, _translator, _guild_id, _duration_ms):
                if value is None:
                    return None
                try:
                    return f"{float(value):.3f}"
                except (TypeError, ValueError):
                    return None

            def _format_boolean(value, _scan_result, translator, guild_id, _duration_ms):
                if value is None:
                    return None
                return localize_boolean(translator, bool(value), guild_id)

            def _format_summary(value, _scan_result, _translator, _guild_id, _duration_ms):
                if value is None:
                    return None
                return str(value)

            def _format_video_frames(scanned, scan_result, _translator, _guild_id, _duration_ms):
                target = scan_result.get("video_frames_target")
                if scanned is None or target is None:
                    return None
                return f"{scanned}/{target}"

            def _format_average_latency(scanned, _scan_result, _translator, _guild_id, duration_ms_param):
                if scanned is None:
                    return None
                try:
                    scanned_frames = float(scanned)
                except (TypeError, ValueError):
                    return None
                if scanned_frames <= 0:
                    return None
                average_latency_per_frame = duration_ms_param / scanned_frames
                return f"{average_latency_per_frame:.2f} ms/frame"

            field_specs: tuple[ScanFieldSpec, ...] = (
                ScanFieldSpec(field_key="reason", inline=False, formatter=_format_reason),
                ScanFieldSpec(field_key="category", formatter=_format_category),
                ScanFieldSpec(field_key="score", formatter=_format_float),
                ScanFieldSpec(field_key="flagged_any", formatter=_format_boolean),
                ScanFieldSpec(
                    field_key="summary_categories",
                    inline=False,
                    formatter=_format_summary,
                ),
                ScanFieldSpec(field_key="max_similarity", formatter=_format_float),
                ScanFieldSpec(field_key="max_category", formatter=_format_summary),
                ScanFieldSpec(field_key="similarity", formatter=_format_float),
                ScanFieldSpec(field_key="high_accuracy", formatter=_format_boolean),
                ScanFieldSpec(field_key="clip_threshold", formatter=_format_float),
                ScanFieldSpec(
                    field_key="moderation_threshold",
                    source_key="threshold",
                    formatter=_format_float,
                ),
                ScanFieldSpec(
                    field_key="video_frames",
                    source_key="video_frames_scanned",
                    formatter=_format_video_frames,
                ),
                ScanFieldSpec(
                    field_key="average_latency_per_frame_ms",
                    source_key="video_frames_scanned",
                    formatter=_format_average_latency,
                ),
            )

            latency_overrides = {
                "breakdown_kwargs": {
                    "bullet": "•",
                    "decimals": 2,
                    "include_step_label": True,
                    "sort_desc": True,
                    "step_wrapper": lambda step: f"`{step}`",
                }
            }

            color_resolver = lambda decision, _result: (
                discord.Color.red()
                if decision == "nsfw"
                else (
                    discord.Color.orange()
                    if decision == "safe"
                    else discord.Color.dark_grey()
                )
            )

            await emit_verbose_report(
                scanner,
                message=message,
                author=author,
                guild_id=guild_id,
                file_type=file_type,
                detected_mime=detected_mime,
                scan_result=scan_result,
                duration_ms=duration_ms,
                filename=filename,
                bold_labels=False,
                latency_kwargs=latency_overrides,
                field_specs=field_specs,
                include_cache_status=False,
                color_resolver=color_resolver,
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
            category_label = localize_category(
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
