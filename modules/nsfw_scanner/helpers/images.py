import asyncio
import io
import os
import random
import traceback
from dataclasses import dataclass
from typing import Any, List, Optional, Sequence

from PIL import Image

from cogs.nsfw import NSFW_CATEGORY_SETTING
from modules.utils import clip_vectors, mysql

from ..constants import (
    CLIP_THRESHOLD,
    HIGH_ACCURACY_SIMILARITY,
    VECTOR_REFRESH_DIVISOR,
)
from ..utils.categories import is_allowed_category
from ..utils.file_ops import safe_delete
from ..utils.frames import ExtractedFrame
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
        image_bytes=image_bytes,
        image_mime=image_mime,
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

    image: Image.Image | None = None
    try:
        image = await _open_image_from_path(original_filename)
        _, ext = os.path.splitext(original_filename)
        needs_conversion = convert_to_png and ext.lower() != ".png"
        image_path: str | None = None if needs_conversion else original_filename
        image_bytes: bytes | None = None
        image_mime: str | None = None

        if needs_conversion:
            image_bytes = await asyncio.to_thread(_encode_image_to_png_bytes, image)
            image_mime = "image/png"

        return await _run_image_pipeline(
            scanner,
            image_path=image_path,
            image=image,
            context=ctx,
            similarity_response=similarity_response,
            image_bytes=image_bytes,
            image_mime=image_mime,
        )
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
) -> list[tuple[ExtractedFrame, dict[str, Any] | None]]:
    """
    Analyse a batch of in-memory frames using shared settings/context.
    Returns list of (frame_data, result_dict).
    """
    prepared: list[tuple[ExtractedFrame, Image.Image | None]] = []
    valid_images: list[Image.Image] = []

    decode_tasks: list[asyncio.Task[Image.Image | None]] = []
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

    decoded_images = await asyncio.gather(*decode_tasks)

    for frame, image in zip(frame_payloads, decoded_images):
        prepared.append((frame, image))
        if image is not None:
            valid_images.append(image)

    similarity_batches: List[List[dict[str, Any]]] = []
    if valid_images:
        similarity_batches = await asyncio.to_thread(
            clip_vectors.query_similar_batch, valid_images, 0
        )

    results: list[tuple[ExtractedFrame, dict[str, Any] | None]] = []
    similarity_iter = iter(similarity_batches)

    for frame, image in prepared:
        response: dict[str, Any] | None = None
        similarity_response = next(similarity_iter, []) if image is not None else None
        payload_bytes = frame.data
        payload_mime = frame.mime_type
        if convert_to_png and image is not None and frame.mime_type.lower() != "image/png":
            payload_bytes = await asyncio.to_thread(_encode_image_to_png_bytes, image)
            payload_mime = "image/png"
        if image is not None:
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
                image.close()
        results.append((frame, response))

    return results
