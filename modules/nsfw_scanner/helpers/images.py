import asyncio
import io
import logging
import mimetypes
import os
import random
import time
import traceback
from dataclasses import dataclass
from typing import Any, List, Optional, Sequence

from PIL import Image

try:
    from pillow_heif import register_heif_opener
except ImportError:  # pragma: no cover - optional dependency handled gracefully
    register_heif_opener = None
else:
    register_heif_opener()

from cogs.nsfw import NSFW_CATEGORY_SETTING
from modules.utils import clip_vectors, mysql
from modules.utils.log_channel import send_log_message

from ..constants import (
    CLIP_THRESHOLD,
    HIGH_ACCURACY_SIMILARITY,
    LOG_CHANNEL_ID,
    MOD_API_MAX_CONCURRENCY,
    VECTOR_REFRESH_DIVISOR,
)
from ..utils.categories import is_allowed_category
from ..utils.file_ops import safe_delete
from ..utils.frames import ExtractedFrame
from .metrics import merge_latency_breakdown
from .moderation import moderator_api


_MODERATION_API_SEMAPHORE = asyncio.Semaphore(max(1, MOD_API_MAX_CONCURRENCY))

_PNG_PASSTHROUGH_EXTS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".jfif",
    ".webp",
}
_PNG_PASSTHROUGH_FORMATS = {
    "PNG",
    "JPEG",
    "JPG",
    "JFIF",
    "WEBP",
}


log = logging.getLogger(__name__)


@dataclass(slots=True)
class ImageProcessingContext:
    guild_id: int | None
    settings_map: dict[str, Any]
    allowed_categories: list[str]
    moderation_threshold: float
    high_accuracy: bool
    accelerated: bool


async def build_image_processing_context(
    guild_id: int | None,
    settings: dict[str, Any] | None = None,
    accelerated: bool | None = None,
) -> ImageProcessingContext:
    settings_map: dict[str, Any] = settings.copy() if settings else {}

    if not settings_map and guild_id is not None:
        settings_map = await mysql.get_settings(
            guild_id,
            [NSFW_CATEGORY_SETTING, "threshold", "nsfw-high-accuracy"],
        ) or {}

    try:
        moderation_threshold = float(settings_map.get("threshold", 0.7))
    except (TypeError, ValueError):
        moderation_threshold = 0.7

    high_accuracy = bool(settings_map.get("nsfw-high-accuracy"))
    allowed_categories = settings_map.get(NSFW_CATEGORY_SETTING) or []

    accelerated_flag = bool(accelerated)
    if accelerated_flag is False and accelerated is None and guild_id is not None:
        try:
            accelerated_flag = bool(await mysql.is_accelerated(guild_id=guild_id))
        except Exception:
            accelerated_flag = False

    return ImageProcessingContext(
        guild_id=guild_id,
        settings_map=settings_map,
        allowed_categories=list(allowed_categories),
        moderation_threshold=moderation_threshold,
        high_accuracy=high_accuracy,
        accelerated=accelerated_flag,
    )


async def _open_image_from_path(path: str) -> Image.Image:
    def _load() -> Image.Image:
        image = Image.open(path)
        try:
            image.load()
            original_format = (image.format or "").upper()
            if image.mode != "RGBA":
                converted = image.convert("RGBA")
                converted.load()
                if original_format:
                    converted.info["original_format"] = original_format
                image.close()
                image = converted
            elif original_format:
                image.info["original_format"] = original_format
            return image
        except Exception:
            image.close()
            raise

    return await asyncio.to_thread(_load)


async def _notify_image_open_failure(
    scanner,
    *,
    filename: str,
    exc: Exception,
) -> None:
    if not LOG_CHANNEL_ID:
        return

    bot = getattr(scanner, "bot", None)
    if bot is None:
        return

    try:
        display_name = os.path.basename(filename) or filename
    except Exception:
        display_name = filename

    error_summary = f"{type(exc).__name__}: {exc}"
    message = (
        ":warning: Failed to open image during NSFW scan. "
        f"File: `{display_name}`. Error: `{error_summary}`"
    )

    try:
        success = await send_log_message(
            bot,
            content=message,
            context="nsfw_scanner.image_open",
        )
    except Exception:  # pragma: no cover - best effort logging
        log.debug(
            "Failed to report image open failure to LOG_CHANNEL_ID=%s",
            LOG_CHANNEL_ID,
            exc_info=True,
        )
        return

    if not success:
        log.debug(
            "Failed to report image open failure to LOG_CHANNEL_ID=%s",
            LOG_CHANNEL_ID,
        )


async def _open_image_from_bytes(data: bytes) -> Image.Image:
    def _load() -> Image.Image:
        buffer = io.BytesIO(data)
        image = Image.open(buffer)
        try:
            image.load()
            original_format = (image.format or "").upper()
            if image.mode != "RGBA":
                converted = image.convert("RGBA")
                converted.load()
                if original_format:
                    converted.info["original_format"] = original_format
                image.close()
                image = converted
            elif original_format:
                image.info["original_format"] = original_format
            return image
        finally:
            buffer.close()

    return await asyncio.to_thread(_load)


def _encode_image_to_png_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    try:
        image.save(buffer, format="PNG")
        return buffer.getvalue()
    finally:
        buffer.close()


async def _run_image_pipeline(
    scanner,
    *,
    image_path: str | None,
    image: Image.Image,
    context: ImageProcessingContext,
    similarity_response: Optional[List[dict[str, Any]]] = None,
    image_bytes: bytes | None = None,
    image_mime: str | None = None,
    payload_metadata: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    total_started = time.perf_counter()
    latency_steps: dict[str, dict[str, Any]] = {}

    def _add_step(name: str, duration: float | None, *, label: str | None = None) -> None:
        if duration is None:
            return
        try:
            duration_value = float(duration)
        except (TypeError, ValueError):
            return
        duration_value = max(duration_value, 0.0)
        if duration_value == 0:
            return
        entry = latency_steps.setdefault(
            name,
            {
                "duration_ms": 0.0,
                "label": label or name.replace("_", " ").title(),
            },
        )
        entry["duration_ms"] = float(entry.get("duration_ms") or 0.0) + duration_value
        if label:
            entry["label"] = label
        elif not entry.get("label"):
            entry["label"] = name.replace("_", " ").title()

    def _finalize(payload: dict[str, Any]) -> dict[str, Any]:
        total_duration = max((time.perf_counter() - total_started) * 1000, 0.0)
        pipeline_metrics = payload.setdefault("pipeline_metrics", {})
        pipeline_metrics["latency_breakdown_ms"] = merge_latency_breakdown(
            pipeline_metrics.get("latency_breakdown_ms"),
            latency_steps,
        )
        current_total = float(pipeline_metrics.get("total_latency_ms") or 0.0)
        pipeline_metrics["total_latency_ms"] = max(current_total, total_duration)
        return payload

    similarity_results = similarity_response
    if similarity_results is None:
        similarity_started = time.perf_counter()
        try:
            similarity_results = await asyncio.to_thread(
                clip_vectors.query_similar, image, threshold=0
            )
        except Exception as exc:
            log.warning(
                "Similarity search failed; falling back to moderator API: %s",
                exc,
                exc_info=True,
            )
            similarity_results = []
        finally:
            _add_step(
                "similarity_search",
                (time.perf_counter() - similarity_started) * 1000,
                label="Similarity Search",
            )

    best_match = None
    max_similarity = 0.0
    max_category = None
    if similarity_results:
        best_match = max(
            similarity_results,
            key=lambda item: float(item.get("similarity", 0) or 0),
        )
        max_similarity = float(best_match.get("similarity", 0) or 0)
        max_category = best_match.get("category")

    refresh_triggered = (
        best_match
        and not context.accelerated
        and VECTOR_REFRESH_DIVISOR > 0
        and random.randint(1, VECTOR_REFRESH_DIVISOR) == 1
    )
    if refresh_triggered:
        vector_id = best_match.get("vector_id")
        if vector_id is not None:
            try:
                delete_started = time.perf_counter()
                await clip_vectors.delete_vectors([vector_id])
                duration = (time.perf_counter() - delete_started) * 1000
                if duration > 0:
                    entry = latency_steps.setdefault(
                        "vector_delete",
                        {
                            "duration_ms": 0.0,
                            "label": "Vector Delete",
                        },
                    )
                    entry["duration_ms"] = float(entry.get("duration_ms") or 0.0) + duration
            except Exception as exc:
                print(
                    f"[process_image] Failed to delete vector {vector_id}: {exc}"
                )

    milvus_available = clip_vectors.is_available()
    allow_similarity_shortcut = (
        similarity_results
        and not refresh_triggered
        and (
            not context.high_accuracy
            or max_similarity >= HIGH_ACCURACY_SIMILARITY
        )
    )

    if allow_similarity_shortcut:
        for item in similarity_results:
            similarity = float(item.get("similarity", 0) or 0)
            if similarity < CLIP_THRESHOLD:
                continue

            category = item.get("category")
            if not category:
                result = {
                    "is_nsfw": False,
                    "reason": "similarity_match",
                    "max_similarity": max_similarity,
                    "max_category": max_category,
                    "high_accuracy": context.high_accuracy,
                    "clip_threshold": CLIP_THRESHOLD,
                    "similarity": similarity,
                }
                return _finalize(result)

            if is_allowed_category(category, context.allowed_categories):
                result = {
                    "is_nsfw": True,
                    "category": category,
                    "reason": "similarity_match",
                    "max_similarity": max_similarity,
                    "max_category": max_category,
                    "high_accuracy": context.high_accuracy,
                    "clip_threshold": CLIP_THRESHOLD,
                    "similarity": similarity,
                }
                return _finalize(result)

    skip_vector = (
        max_similarity >= CLIP_THRESHOLD and not refresh_triggered
    ) or not milvus_available
    moderation_started = time.perf_counter()
    response = await moderator_api(
        scanner,
        image_path=image_path,
        image_bytes=image_bytes,
        image_mime=image_mime,
        guild_id=context.guild_id,
        image=image,
        skip_vector_add=skip_vector,
        max_similarity=max_similarity,
        allowed_categories=context.allowed_categories,
        threshold=context.moderation_threshold,
        payload_metadata=payload_metadata,
    )
    _add_step(
        "moderation_api",
        (time.perf_counter() - moderation_started) * 1000,
        label="Moderator API",
    )
    if isinstance(response, dict):
        pipeline_metrics = response.setdefault("pipeline_metrics", {})
        if isinstance(pipeline_metrics, dict):
            breakdown = pipeline_metrics.get("moderator_breakdown_ms")
            if breakdown:
                latency_steps = merge_latency_breakdown(latency_steps, breakdown)
        response.setdefault("max_similarity", max_similarity)
        response.setdefault("max_category", max_category)
        response.setdefault("high_accuracy", context.high_accuracy)
        response.setdefault("clip_threshold", CLIP_THRESHOLD)
        return _finalize(response)
    return response


async def process_image(
    scanner,
    original_filename: str,
    guild_id: int | None = None,
    clean_up: bool = True,
    settings: dict[str, Any] | None = None,
    accelerated: bool | None = None,
    *,
    convert_to_png: bool = True,
    context: ImageProcessingContext | None = None,
    similarity_response: Optional[List[dict[str, Any]]] = None,
) -> dict[str, Any] | None:
    overall_started = time.perf_counter()
    ctx = context
    if ctx is None:
        ctx = await build_image_processing_context(
            guild_id,
            settings=settings,
            accelerated=accelerated,
        )

    image: Image.Image | None = None
    latency_steps: dict[str, dict[str, Any]] = {}
    try:
        load_started = time.perf_counter()
        image = await _open_image_from_path(original_filename)
        load_duration = max((time.perf_counter() - load_started) * 1000, 0.0)
        if load_duration > 0:
            entry = latency_steps.setdefault(
                "image_open",
                {
                    "duration_ms": 0.0,
                    "label": "Open Image",
                },
            )
            entry["duration_ms"] = float(entry.get("duration_ms") or 0.0) + load_duration
        _, ext = os.path.splitext(original_filename)
        ext = ext.lower()
        image_info = getattr(image, "info", {}) if image is not None else {}
        original_format = str(image_info.get("original_format") or "").upper()
        passthrough = ext in _PNG_PASSTHROUGH_EXTS or (
            original_format and original_format in _PNG_PASSTHROUGH_FORMATS
        )
        needs_conversion = convert_to_png and not passthrough
        image_path: str | None = None if needs_conversion else original_filename
        image_bytes: bytes | None = None
        image_mime: str | None = None
        if not needs_conversion:
            image_mime = Image.MIME.get(original_format)
            if image_mime is None:
                guessed_mime, _ = mimetypes.guess_type(original_filename)
                image_mime = guessed_mime

        payload_metadata: dict[str, Any] = {
            "input_kind": "image",
            "source_extension": ext or None,
            "original_format": original_format or None,
            "image_mode": getattr(image, "mode", None),
            "image_size": list(image.size) if image else None,
            "conversion_performed": needs_conversion,
            "payload_mime": None,
            "passthrough": not needs_conversion,
        }
        try:
            payload_metadata["source_bytes"] = os.path.getsize(original_filename)
        except OSError:
            payload_metadata["source_bytes"] = None

        conversion_reason: str | None = None

        if needs_conversion:
            encode_started = time.perf_counter()
            image_bytes = await asyncio.to_thread(_encode_image_to_png_bytes, image)
            image_mime = "image/png"
            encode_duration = max((time.perf_counter() - encode_started) * 1000, 0.0)
            if encode_duration > 0:
                entry = latency_steps.setdefault(
                    "image_encode",
                    {
                        "duration_ms": 0.0,
                        "label": "Encode PNG",
                    },
                )
                entry["duration_ms"] = float(entry.get("duration_ms") or 0.0) + encode_duration
            conversion_reason = "unsupported_format"
            payload_metadata["payload_bytes"] = len(image_bytes or b"")
            payload_metadata["payload_mime"] = image_mime
            payload_metadata["conversion_target"] = "image/png"
            payload_metadata["encode_duration_ms"] = encode_duration
        else:
            payload_metadata["payload_mime"] = image_mime
            payload_metadata["payload_bytes"] = payload_metadata.get("source_bytes")
            payload_metadata["conversion_target"] = None

        payload_metadata["conversion_reason"] = conversion_reason

        response = await _run_image_pipeline(
            scanner,
            image_path=image_path,
            image=image,
            context=ctx,
            similarity_response=similarity_response,
            image_bytes=image_bytes,
            image_mime=image_mime,
            payload_metadata=payload_metadata,
        )
        if isinstance(response, dict):
            pipeline_metrics = response.setdefault("pipeline_metrics", {})
            pipeline_metrics["latency_breakdown_ms"] = merge_latency_breakdown(
                pipeline_metrics.get("latency_breakdown_ms"),
                latency_steps,
            )
            current_total = float(pipeline_metrics.get("total_latency_ms") or 0.0)
            pipeline_metrics["total_latency_ms"] = max(
                current_total,
                max((time.perf_counter() - overall_started) * 1000, 0.0),
            )
        return response
    except Exception as exc:
        print(traceback.format_exc())
        print(f"[process_image] Error processing image {original_filename}: {exc}")
        if image is None:
            await _notify_image_open_failure(
                scanner,
                filename=original_filename,
                exc=exc,
            )
        return None
    finally:
        if image is not None:
            try:
                image.close()
            except Exception:
                pass
        if clean_up:
            safe_delete(original_filename)


async def process_image_batch(
    scanner,
    frame_payloads: Sequence[ExtractedFrame],
    context: ImageProcessingContext,
    *,
    convert_to_png: bool = False,
) -> tuple[
    list[tuple[ExtractedFrame, dict[str, Any] | None]],
    dict[str, float],
]:
    """
    Analyse a batch of in-memory frames using shared settings/context.
    Returns list of (frame_data, result_dict).
    """
    prepared: list[tuple[ExtractedFrame, Image.Image | None]] = []
    valid_images: list[Image.Image] = []

    decode_tasks: list[asyncio.Task[Image.Image | None]] = []
    batch_metrics: dict[str, float] = {
        "decode_latency_ms": 0.0,
        "similarity_latency_ms": 0.0,
        "moderation_latency_ms": 0.0,
        "moderation_wait_latency_ms": 0.0,
    }
    semaphore = asyncio.Semaphore(16 if context.accelerated else 1)

    async def _decode_frame(frame: ExtractedFrame) -> Image.Image | None:
        async with semaphore:
            try:
                return await _open_image_from_bytes(frame.data)
            except Exception as exc:
                print(f"[process_image_batch] Failed to open {frame.name}: {exc}")
                return None

    for frame in frame_payloads:
        decode_tasks.append(asyncio.create_task(_decode_frame(frame)))

    decode_started = time.perf_counter()
    decoded_images = await asyncio.gather(*decode_tasks)
    batch_metrics["decode_latency_ms"] += (
        time.perf_counter() - decode_started
    ) * 1000

    for frame, image in zip(frame_payloads, decoded_images):
        prepared.append((frame, image))
        if image is not None:
            valid_images.append(image)

    similarity_batches: List[List[dict[str, Any]]] = []
    if valid_images:
        try:
            similarity_started = time.perf_counter()
            similarity_batches = await asyncio.to_thread(
                clip_vectors.query_similar_batch, valid_images, 0
            )
            batch_metrics["similarity_latency_ms"] += (
                time.perf_counter() - similarity_started
            ) * 1000
        except Exception as exc:
            log.warning(
                "Batch similarity search failed; continuing without matches: %s",
                exc,
                exc_info=True,
            )
            similarity_batches = [[] for _ in valid_images]

    results: list[tuple[ExtractedFrame, dict[str, Any] | None]] = []
    similarity_iter = iter(similarity_batches)

    entries: list[
        tuple[
            ExtractedFrame,
            Image.Image | None,
            bytes | None,
            str | None,
            Optional[List[dict[str, Any]]],
            dict[str, Any] | None,
        ]
    ] = []

    for frame, image in prepared:
        similarity_response = next(similarity_iter, []) if image is not None else None
        payload_bytes: bytes | None = frame.data
        payload_mime: str | None = frame.mime_type
        frame_mime_lower = (frame.mime_type or "").lower()
        conversion_performed = (
            convert_to_png and image is not None and frame_mime_lower != "image/png"
        )
        if conversion_performed:
            payload_bytes = await asyncio.to_thread(_encode_image_to_png_bytes, image)
            payload_mime = "image/png"
        payload_metadata: dict[str, Any] | None = {
            "input_kind": "image",
            "frame_name": frame.name,
            "source_extension": os.path.splitext(frame.name)[1].lower() if frame.name else None,
            "original_format": getattr(image, "info", {}).get("original_format") if image is not None else None,
            "original_mime": frame.mime_type,
            "conversion_performed": conversion_performed,
            "payload_mime": payload_mime,
            "passthrough": not conversion_performed,
            "image_size": list(image.size) if image is not None else None,
            "image_mode": getattr(image, "mode", None) if image is not None else None,
            "source_bytes": len(frame.data) if frame.data is not None else None,
            "payload_bytes": len(payload_bytes) if payload_bytes is not None else (len(frame.data) if frame.data is not None else None),
            "conversion_target": "image/png"
            if conversion_performed
            else frame.mime_type,
            "conversion_reason": "unsupported_format" if conversion_performed else None,
        }
        entries.append((frame, image, payload_bytes, payload_mime, similarity_response, payload_metadata))

    async def _moderate_entry(
        frame: ExtractedFrame,
        image: Image.Image | None,
        payload_bytes: bytes | None,
        payload_mime: str | None,
        similarity_response: Optional[List[dict[str, Any]]],
        payload_metadata: dict[str, Any] | None,
    ) -> tuple[ExtractedFrame, dict[str, Any] | None]:
        response: dict[str, Any] | None = None
        if image is not None:
            try:
                wait_started = time.perf_counter()
                async with _MODERATION_API_SEMAPHORE:
                    acquired_at = time.perf_counter()
                    batch_metrics["moderation_wait_latency_ms"] += (
                        acquired_at - wait_started
                    ) * 1000
                    response = await _run_image_pipeline(
                        scanner,
                        image_path=None,
                        image=image,
                        context=context,
                        similarity_response=similarity_response,
                        image_bytes=payload_bytes,
                        image_mime=payload_mime,
                        payload_metadata=payload_metadata,
                    )
                    batch_metrics["moderation_latency_ms"] += (
                        time.perf_counter() - acquired_at
                    ) * 1000
            finally:
                image.close()
        return frame, response

    if entries:
        results.extend(
            await asyncio.gather(
                *(
                    _moderate_entry(
                        frame,
                        image,
                        payload_bytes,
                        payload_mime,
                        similarity_response,
                        payload_metadata,
                    )
                    for frame, image, payload_bytes, payload_mime, similarity_response, payload_metadata in entries
                )
            )
        )

    return results, batch_metrics
