import asyncio
import io
import os
import random
import time
import traceback
from dataclasses import dataclass
from typing import Any, List, Optional, Sequence

from PIL import Image

from cogs.nsfw import NSFW_CATEGORY_SETTING
from modules.utils import clip_vectors, mysql
from modules.config.premium_plans import PLAN_CORE, PLAN_FREE

from ..constants import (
    CLIP_THRESHOLD,
    HIGH_ACCURACY_SIMILARITY,
    VECTOR_REFRESH_DIVISOR,
)
from ..utils.categories import is_allowed_category
from ..utils.file_ops import safe_delete
from ..utils.frames import ExtractedFrame
from ..limits import PremiumLimits, resolve_limits
from .moderation import moderator_api


_PLAN_SEMAPHORES: dict[str, tuple[int, asyncio.Semaphore]] = {}


def _get_plan_semaphore(plan: str, limit: int) -> asyncio.Semaphore:
    safe_plan = plan or PLAN_FREE
    safe_limit = max(1, limit)
    cached = _PLAN_SEMAPHORES.get(safe_plan)
    if cached and cached[0] == safe_limit:
        return cached[1]
    semaphore = asyncio.Semaphore(safe_limit)
    _PLAN_SEMAPHORES[safe_plan] = (safe_limit, semaphore)
    return semaphore


@dataclass(slots=True)
class ImageProcessingContext:
    guild_id: int | None
    settings_map: dict[str, Any]
    allowed_categories: list[str]
    moderation_threshold: float
    high_accuracy: bool
    limits: PremiumLimits

    @property
    def accelerated(self) -> bool:
        return self.limits.is_premium


async def build_image_processing_context(
    guild_id: int | None,
    settings: dict[str, Any] | None = None,
    limits: PremiumLimits | None = None,
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

    if limits is not None:
        resolved_limits = limits
    else:
        fallback_plan = PLAN_CORE if accelerated else PLAN_FREE
        resolved_limits = resolve_limits(fallback_plan)

    return ImageProcessingContext(
        guild_id=guild_id,
        settings_map=settings_map,
        allowed_categories=list(allowed_categories),
        moderation_threshold=moderation_threshold,
        high_accuracy=high_accuracy,
        limits=resolved_limits,
    )


async def _open_image_from_path(path: str) -> Image.Image:
    def _load() -> Image.Image:
        image = Image.open(path)
        try:
            image.load()
            if image.mode != "RGBA":
                converted = image.convert("RGBA")
                converted.load()
                image.close()
                image = converted
            return image
        except Exception:
            image.close()
            raise

    return await asyncio.to_thread(_load)


async def _open_image_from_bytes(data: bytes) -> Image.Image:
    def _load() -> Image.Image:
        buffer = io.BytesIO(data)
        image = Image.open(buffer)
        try:
            image.load()
            if image.mode != "RGBA":
                converted = image.convert("RGBA")
                converted.load()
                image.close()
                image = converted
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
        existing_breakdown = pipeline_metrics.get("latency_breakdown_ms")
        if not isinstance(existing_breakdown, dict):
            existing_breakdown = {}
        else:
            normalized: dict[str, dict[str, Any]] = {}
            for step_key, step_value in existing_breakdown.items():
                if isinstance(step_value, dict):
                    normalized_duration = step_value.get("duration_ms")
                    try:
                        normalized_duration = float(normalized_duration)
                    except (TypeError, ValueError):
                        continue
                    normalized[step_key] = {
                        "duration_ms": normalized_duration,
                        "label": step_value.get("label"),
                    }
                elif isinstance(step_value, (int, float)):
                    normalized[step_key] = {
                        "duration_ms": float(step_value),
                        "label": step_key.replace("_", " ").title(),
                    }
            existing_breakdown = normalized
        for step_name, entry in latency_steps.items():
            duration_val = entry.get("duration_ms")
            try:
                duration_float = float(duration_val)
            except (TypeError, ValueError):
                continue
            if duration_float <= 0:
                continue
            label_value = entry.get("label") or step_name.replace("_", " ").title()
            existing_entry = existing_breakdown.get(step_name)
            if isinstance(existing_entry, dict):
                try:
                    duration_float += float(existing_entry.get("duration_ms") or 0.0)
                except (TypeError, ValueError):
                    pass
                if not label_value:
                    label_value = existing_entry.get("label")
            elif isinstance(existing_entry, (int, float)):
                duration_float += float(existing_entry)
            existing_breakdown[step_name] = {
                "duration_ms": duration_float,
                "label": label_value or step_name.replace("_", " ").title(),
            }
        pipeline_metrics["latency_breakdown_ms"] = existing_breakdown
        current_total = float(pipeline_metrics.get("total_latency_ms") or 0.0)
        pipeline_metrics["total_latency_ms"] = max(current_total, total_duration)
        return payload

    similarity_results = similarity_response
    if similarity_results is None:
        similarity_started = time.perf_counter()
        similarity_results = await asyncio.to_thread(
            clip_vectors.query_similar, image, threshold=0
        )
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
                await clip_vectors.delete_vectors([vector_id])
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
        latency_callback=_add_step,
    )
    _add_step(
        "moderation_api",
        (time.perf_counter() - moderation_started) * 1000,
        label="Moderator API",
    )
    if isinstance(response, dict):
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
        needs_conversion = convert_to_png and ext.lower() != ".png"
        image_path: str | None = None if needs_conversion else original_filename
        image_bytes: bytes | None = None
        image_mime: str | None = None

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

        response = await _run_image_pipeline(
            scanner,
            image_path=image_path,
            image=image,
            context=ctx,
            similarity_response=similarity_response,
            image_bytes=image_bytes,
            image_mime=image_mime,
        )
        if isinstance(response, dict):
            pipeline_metrics = response.setdefault("pipeline_metrics", {})
            breakdown = pipeline_metrics.get("latency_breakdown_ms")
            if not isinstance(breakdown, dict):
                breakdown = {}
            else:
                normalized_breakdown: dict[str, dict[str, Any]] = {}
                for step_key, step_value in breakdown.items():
                    if isinstance(step_value, dict):
                        normalized_duration = step_value.get("duration_ms")
                        try:
                            normalized_duration = float(normalized_duration)
                        except (TypeError, ValueError):
                            continue
                        normalized_breakdown[step_key] = {
                            "duration_ms": normalized_duration,
                            "label": step_value.get("label"),
                        }
                    elif isinstance(step_value, (int, float)):
                        normalized_breakdown[step_key] = {
                            "duration_ms": float(step_value),
                            "label": step_key.replace("_", " ").title(),
                        }
                breakdown = normalized_breakdown
            for step_name, entry in latency_steps.items():
                duration_val = entry.get("duration_ms")
                try:
                    duration_float = float(duration_val)
                except (TypeError, ValueError):
                    continue
                if duration_float <= 0:
                    continue
                label_value = entry.get("label") or step_name.replace("_", " ").title()
                existing_entry = breakdown.get(step_name)
                if isinstance(existing_entry, dict):
                    try:
                        duration_float += float(existing_entry.get("duration_ms") or 0.0)
                    except (TypeError, ValueError):
                        pass
                    if not label_value:
                        label_value = existing_entry.get("label")
                elif isinstance(existing_entry, (int, float)):
                    duration_float += float(existing_entry)
                breakdown[step_name] = {
                    "duration_ms": duration_float,
                    "label": label_value or step_name.replace("_", " ").title(),
                }
            pipeline_metrics["latency_breakdown_ms"] = breakdown
            current_total = float(pipeline_metrics.get("total_latency_ms") or 0.0)
            pipeline_metrics["total_latency_ms"] = max(
                current_total,
                max((time.perf_counter() - overall_started) * 1000, 0.0),
            )
        return response
    except Exception as exc:
        print(traceback.format_exc())
        print(f"[process_image] Error processing image {original_filename}: {exc}")
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
    metrics: dict[str, Any] | None = None,
    latency_steps: dict[str, dict[str, Any]] | None = None,
) -> list[tuple[ExtractedFrame, dict[str, Any] | None]]:
    """
    Analyse a batch of in-memory frames using shared settings/context.
    Returns list of (frame_data, result_dict).
    """
    prepared: list[tuple[ExtractedFrame, Image.Image | None]] = []
    valid_images: list[Image.Image] = []

    decode_tasks: list[asyncio.Task[Image.Image | None]] = []
    semaphore = asyncio.Semaphore(16 if context.accelerated else 1)

    def _record_latency(
        metrics_key: str,
        step_key: str,
        duration_ms: float,
        *,
        label: str,
    ) -> None:
        if duration_ms <= 0:
            return
        if metrics is not None:
            metrics[metrics_key] = float(metrics.get(metrics_key) or 0.0) + duration_ms
        if latency_steps is not None:
            entry = latency_steps.setdefault(
                step_key,
                {
                    "duration_ms": 0.0,
                    "label": label,
                },
            )
            entry["duration_ms"] = float(entry.get("duration_ms") or 0.0) + duration_ms
            if not entry.get("label"):
                entry["label"] = label

    async def _decode_frame(frame: ExtractedFrame) -> Image.Image | None:
        async with semaphore:
            try:
                return await _open_image_from_bytes(frame.data)
            except Exception as exc:
                print(f"[process_image_batch] Failed to open {frame.name}: {exc}")
                return None

    decode_started = time.perf_counter()
    for frame in frame_payloads:
        decode_tasks.append(asyncio.create_task(_decode_frame(frame)))

    decoded_images = await asyncio.gather(*decode_tasks)
    decode_duration = max((time.perf_counter() - decode_started) * 1000, 0.0)
    _record_latency(
        "frame_pipeline_decode_ms",
        "frame_pipeline_decode",
        decode_duration,
        label="Frame Decode",
    )

    for frame, image in zip(frame_payloads, decoded_images):
        prepared.append((frame, image))
        if image is not None:
            valid_images.append(image)

    similarity_batches: List[List[dict[str, Any]]] = []
    if valid_images:
        similarity_started = time.perf_counter()
        similarity_batches = await asyncio.to_thread(
            clip_vectors.query_similar_batch, valid_images, 0
        )
        similarity_duration = max((time.perf_counter() - similarity_started) * 1000, 0.0)
        _record_latency(
            "frame_pipeline_similarity_ms",
            "frame_pipeline_similarity",
            similarity_duration,
            label="Frame Similarity Search",
        )

    results: list[tuple[ExtractedFrame, dict[str, Any] | None]] = []
    similarity_iter = iter(similarity_batches)

    entries: list[
        tuple[
            ExtractedFrame,
            Image.Image | None,
            bytes | None,
            str | None,
            Optional[List[dict[str, Any]]],
        ]
    ] = []

    for frame, image in prepared:
        similarity_response = next(similarity_iter, []) if image is not None else None
        payload_bytes: bytes | None = frame.data
        payload_mime: str | None = frame.mime_type
        if convert_to_png and image is not None and frame.mime_type.lower() != "image/png":
            payload_bytes = await asyncio.to_thread(_encode_image_to_png_bytes, image)
            payload_mime = "image/png"
        entries.append((frame, image, payload_bytes, payload_mime, similarity_response))

    plan_limits = getattr(context, "limits", None)
    plan_name = getattr(plan_limits, "plan", PLAN_FREE) if plan_limits else PLAN_FREE
    plan_limit = getattr(plan_limits, "max_moderation_calls", 1) if plan_limits else 1
    plan_semaphore = _get_plan_semaphore(plan_name, plan_limit)

    async def _moderate_entry(
        frame: ExtractedFrame,
        image: Image.Image | None,
        payload_bytes: bytes | None,
        payload_mime: str | None,
        similarity_response: Optional[List[dict[str, Any]]],
        semaphore: asyncio.Semaphore,
    ) -> tuple[ExtractedFrame, dict[str, Any] | None]:
        response: dict[str, Any] | None = None
        inference_started = time.perf_counter()
        wait_duration_ms = 0.0
        pipeline_duration_ms = 0.0
        if image is not None:
            try:
                wait_started = time.perf_counter()
                await semaphore.acquire()
                wait_duration_ms = max((time.perf_counter() - wait_started) * 1000, 0.0)
                pipeline_started = time.perf_counter()
                try:
                    response = await _run_image_pipeline(
                        scanner,
                        image_path=None,
                        image=image,
                        context=context,
                        similarity_response=similarity_response,
                        image_bytes=payload_bytes,
                        image_mime=payload_mime,
                    )
                finally:
                    pipeline_duration_ms = max(
                        (time.perf_counter() - pipeline_started) * 1000,
                        0.0,
                    )
                    semaphore.release()
            finally:
                image.close()
        if wait_duration_ms > 0:
            _record_latency(
                "frame_pipeline_inference_wait_ms",
                "frame_pipeline_inference_wait",
                wait_duration_ms,
                label="Frame Semaphore Wait",
            )
        if pipeline_duration_ms > 0:
            _record_latency(
                "frame_pipeline_inference_run_ms",
                "frame_pipeline_inference_run",
                pipeline_duration_ms,
                label="Frame Pipeline Execution",
            )
        inference_duration = max((time.perf_counter() - inference_started) * 1000, 0.0)
        _record_latency(
            "frame_pipeline_inference_ms",
            "frame_pipeline_inference",
            inference_duration,
            label="Frame Inference",
        )
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
                        plan_semaphore,
                    )
                    for frame, image, payload_bytes, payload_mime, similarity_response in entries
                )
            )
        )

    return results
