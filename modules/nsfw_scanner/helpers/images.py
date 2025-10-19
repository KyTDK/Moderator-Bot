import asyncio
import os
import random
import traceback
import uuid
from dataclasses import dataclass
from typing import Any, List, Optional, Sequence

from PIL import Image

from cogs.nsfw import NSFW_CATEGORY_SETTING
from modules.utils import clip_vectors, mysql

from ..constants import (
    CLIP_THRESHOLD,
    HIGH_ACCURACY_SIMILARITY,
    TMP_DIR,
    VECTOR_REFRESH_DIVISOR,
)
from ..utils import convert_to_png_safe, is_allowed_category, safe_delete
from .moderation import moderator_api


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


async def _run_image_pipeline(
    scanner,
    *,
    image_path: str,
    image: Image.Image,
    context: ImageProcessingContext,
    similarity_response: Optional[List[dict[str, Any]]] = None,
) -> dict[str, Any] | None:
    similarity_results = similarity_response
    if similarity_results is None:
        similarity_results = await asyncio.to_thread(
            clip_vectors.query_similar, image, threshold=0
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
                return {
                    "is_nsfw": False,
                    "reason": "similarity_match",
                    "max_similarity": max_similarity,
                    "max_category": max_category,
                    "high_accuracy": context.high_accuracy,
                    "clip_threshold": CLIP_THRESHOLD,
                    "similarity": similarity,
                }

            if is_allowed_category(category, context.allowed_categories):
                return {
                    "is_nsfw": True,
                    "category": category,
                    "reason": "similarity_match",
                    "max_similarity": max_similarity,
                    "max_category": max_category,
                    "high_accuracy": context.high_accuracy,
                    "clip_threshold": CLIP_THRESHOLD,
                    "similarity": similarity,
                }

    skip_vector = (
        max_similarity >= CLIP_THRESHOLD and not refresh_triggered
    ) or not milvus_available
    response = await moderator_api(
        scanner,
        image_path=image_path,
        guild_id=context.guild_id,
        image=image,
        skip_vector_add=skip_vector,
        max_similarity=max_similarity,
        allowed_categories=context.allowed_categories,
        threshold=context.moderation_threshold,
    )
    if isinstance(response, dict):
        response.setdefault("max_similarity", max_similarity)
        response.setdefault("max_category", max_category)
        response.setdefault("high_accuracy", context.high_accuracy)
        response.setdefault("clip_threshold", CLIP_THRESHOLD)
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
    ctx = context
    if ctx is None:
        ctx = await build_image_processing_context(
            guild_id,
            settings=settings,
            accelerated=accelerated,
        )

    _, ext = os.path.splitext(original_filename)
    needs_conversion = convert_to_png and ext.lower() != ".png"
    if needs_conversion:
        png_converted_path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex[:12]}.png")
        conversion_result = await asyncio.to_thread(
            convert_to_png_safe, original_filename, png_converted_path
        )
        if not conversion_result:
            print(f"[process_image] PNG conversion failed: {original_filename}")
            return None
        image_path = conversion_result
    else:
        png_converted_path = None
        image_path = original_filename

    try:
        with Image.open(image_path) as image_file:
            image = image_file
            converted_image = None
            if image.mode != "RGBA":
                converted_image = image.convert("RGBA")
                image = converted_image
            try:
                return await _run_image_pipeline(
                    scanner,
                    image_path=image_path,
                    image=image,
                    context=ctx,
                    similarity_response=similarity_response,
                )
            finally:
                if converted_image is not None:
                    converted_image.close()
    except Exception as exc:
        print(traceback.format_exc())
        print(f"[process_image] Error processing image {original_filename}: {exc}")
        return None
    finally:
        if needs_conversion:
            safe_delete(png_converted_path or "")
        if clean_up:
            safe_delete(original_filename)


async def process_image_batch(
    scanner,
    frame_paths: Sequence[str],
    context: ImageProcessingContext,
    *,
    convert_to_png: bool = False,
) -> list[tuple[str, dict[str, Any] | None]]:
    """
    Analyse a batch of image paths using shared settings/context.
    Returns list of (frame_path, result_dict).
    """
    prepared_images: list[Image.Image] = []
    prepared_paths: list[str] = []
    converted_paths: list[str | None] = []

    for frame_path in frame_paths:
        _, ext = os.path.splitext(frame_path)
        needs_conversion = convert_to_png and ext.lower() != ".png"
        target_path = frame_path
        converted_path = None
        if needs_conversion:
            converted_path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex[:12]}.png")
            conversion_result = await asyncio.to_thread(
                convert_to_png_safe, frame_path, converted_path
            )
            if not conversion_result:
                print(f"[process_image_batch] PNG conversion failed: {frame_path}")
                prepared_images.append(None)  # type: ignore[arg-type]
                prepared_paths.append(frame_path)
                converted_paths.append(converted_path)
                continue
            target_path = conversion_result

        try:
            image_file = Image.open(target_path)
        except Exception as exc:
            print(f"[process_image_batch] Failed to open {frame_path}: {exc}")
            prepared_images.append(None)  # type: ignore[arg-type]
            prepared_paths.append(target_path)
            converted_paths.append(converted_path)
            continue

        converted_image = None
        if image_file.mode != "RGBA":
            converted_image = image_file.convert("RGBA")
            image_file.close()
            image = converted_image
        else:
            image = image_file

        prepared_images.append(image)
        prepared_paths.append(target_path)
        converted_paths.append(converted_path)

    valid_images = [img for img in prepared_images if img is not None]
    similarity_batches: List[List[dict[str, Any]]] = []
    if valid_images:
        similarity_batches = await asyncio.to_thread(
            clip_vectors.query_similar_batch, valid_images, 0
        )

    results: list[tuple[str, dict[str, Any] | None]] = []
    similarity_iter = iter(similarity_batches)

    for frame_path, target_path, img in zip(frame_paths, prepared_paths, prepared_images):
        response: dict[str, Any] | None = None
        similarity_response = next(similarity_iter, []) if img is not None else None
        if img is not None:
            try:
                response = await _run_image_pipeline(
                    scanner,
                    image_path=target_path,
                    image=img,
                    context=context,
                    similarity_response=similarity_response,
                )
            finally:
                img.close()
        results.append((frame_path, response))

    for converted_path in converted_paths:
        if converted_path:
            safe_delete(converted_path)

    return results
