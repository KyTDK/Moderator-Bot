import os
import time
from typing import Any

import discord

from modules.metrics import log_media_scan
from modules.utils import mod_logging, mysql
from modules.utils.localization import localize_message

from modules.nsfw_scanner.helpers.attachments.cache import (
    AttachmentSettingsCache,
    format_queue_wait_label,
)
from modules.nsfw_scanner.helpers.attachments.localization import (
    REPORT_BASE,
    SHARED_ROOT,
    localize_boolean,
    localize_category,
    localize_decision,
    localize_field_name,
    localize_reason,
    resolve_translator,
)
from modules.nsfw_scanner.helpers.attachments.ocr import (
    duplicate_file_for_ocr,
    schedule_async_ocr_text_scan,
)
from modules.nsfw_scanner.helpers.attachments.reporting import build_verbose_scan_embed
from modules.nsfw_scanner.helpers.images import (
    build_image_processing_context,
    process_image,
)
from modules.nsfw_scanner.helpers.ocr import extract_text_from_image
from modules.nsfw_scanner.helpers.metrics import (
    LatencyTracker,
    collect_scan_telemetry,
)
from modules.nsfw_scanner.helpers.videos import process_video
from modules.nsfw_scanner.logging_utils import log_slow_scan_if_needed
from modules.nsfw_scanner.helpers.text_sources import (
    TEXT_SOURCE_OCR,
    normalize_text_sources,
)
from modules.nsfw_scanner.settings_keys import (
    NSFW_OCR_LANGUAGES_SETTING,
    NSFW_TEXT_ENABLED_SETTING,
    NSFW_TEXT_SOURCES_SETTING,
)
from modules.nsfw_scanner.utils.file_types import (
    FILE_TYPE_IMAGE,
    FILE_TYPE_VIDEO,
    determine_file_type,
)
_DEFAULT_OCR_LANGUAGES = ["en"]


def _resolve_ocr_languages(value: Any) -> list[str]:
    if value is None:
        return list(_DEFAULT_OCR_LANGUAGES)

    if isinstance(value, str):
        candidates = [value]
    elif isinstance(value, (list, tuple, set)):
        candidates = list(value)
    else:
        return list(_DEFAULT_OCR_LANGUAGES)

    languages: list[str] = []
    for entry in candidates:
        if entry is None:
            continue
        text = str(entry).strip()
        if not text:
            continue
        languages.append(text[:16])

    return languages or list(_DEFAULT_OCR_LANGUAGES)

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
    queue_name: str | None = None,
) -> bool:
    if settings_cache is None:
        settings_cache = AttachmentSettingsCache()

    queue_label = format_queue_wait_label(queue_name)
    latency_tracker = LatencyTracker(
        started_at=overall_started_at,
        steps=pre_latency_steps,
        queue_label=queue_label,
        queue_name=queue_name,
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
    text_pipeline = getattr(scanner, "_text_pipeline", None)

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
        if queue_name:
            extra_context["queue_name"] = queue_name

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
    if settings is None:
        if guild_id is not None:
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
        else:
            settings = {}

    accelerated_value = await _get_accelerated()
    context_started = time.perf_counter()
    context = await build_image_processing_context(
        guild_id,
        settings=settings,
        accelerated=accelerated_value,
        queue_name=queue_name,
    )
    latency_tracker.record_step(
        "attachment_context_build",
        (time.perf_counter() - context_started) * 1000,
        label="Build Scan Context",
    )
    pipeline_accelerated = context.accelerated

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
        "queue_name": queue_name,
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

    settings_map = context.settings_map or {}
    ocr_languages = _resolve_ocr_languages(settings_map.get(NSFW_OCR_LANGUAGES_SETTING))
    text_sources = normalize_text_sources(settings_map.get(NSFW_TEXT_SOURCES_SETTING))
    ocr_enabled = context.accelerated and (TEXT_SOURCE_OCR in text_sources)

    if (
        file_type == FILE_TYPE_IMAGE
        and not (scan_result and scan_result.get("is_nsfw"))
        and text_pipeline is not None
        and message is not None
        and guild_id is not None
        and ocr_enabled
    ):
        if settings_cache.has_text_enabled():
            text_enabled = bool(settings_cache.get_text_enabled())
        else:
            text_enabled = bool(settings_map.get(NSFW_TEXT_ENABLED_SETTING))
            settings_cache.set_text_enabled(text_enabled)

        if text_enabled:
            stage_started = time.perf_counter()
            staged_path = await duplicate_file_for_ocr(temp_filename)
            latency_tracker.record_step(
                "attachment_ocr_stage",
                (time.perf_counter() - stage_started) * 1000,
                label="Stage OCR Payload",
            )

            if staged_path:
                metadata_overrides = {
                    "ocr_scan": True,
                    "filename": filename,
                    "detected_mime": detected_mime,
                    "source_url": source_url,
                }
                metadata_overrides = {
                    key: value for key, value in metadata_overrides.items() if value is not None
                }
                schedule_async_ocr_text_scan(
                    scanner=scanner,
                    temp_path=staged_path,
                    languages=list(ocr_languages),
                    text_pipeline=text_pipeline,
                    guild_id=guild_id,
                    message=message,
                    nsfw_callback=nsfw_callback,
                    settings_map=dict(context.settings_map or {}),
                    metadata_overrides=metadata_overrides,
                    queue_name=queue_name,
                    perform_actions=perform_actions,
                    accelerated=bool(context.accelerated),
                    extract_text_fn=extract_text_from_image,
                )

    translator = resolve_translator(scanner)

    resolved_total_latency = latency_tracker.total_duration_ms()

    if isinstance(scan_result, dict):
        pipeline_metrics = scan_result.setdefault("pipeline_metrics", {})
        pipeline_metrics, resolved_total_latency = latency_tracker.merge_into_pipeline(
            pipeline_metrics
        )
        scan_result["pipeline_metrics"] = pipeline_metrics
        if pipeline_accelerated is not None:
            pipeline_metrics.setdefault("accelerated", bool(pipeline_accelerated))
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
                verbose_enabled = settings_cache.get_verbose()
            else:
                verbose_enabled = settings.get("nsfw-verbose")
                settings_cache.set_verbose(verbose_enabled)
        if message is not None and verbose_enabled:
            decision_key = "unknown"
            if scan_result is not None:
                decision_key = "nsfw" if scan_result.get("is_nsfw") else "safe"
            embed = build_verbose_scan_embed(
                translator=translator,
                guild_id=guild_id,
                author=author,
                message=message,
                filename=filename,
                file_type=file_type,
                detected_mime=detected_mime,
                decision_key=decision_key,
                scan_result=scan_result,
                telemetry=telemetry,
                total_latency_ms=resolved_total_latency_ms,
            )
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
