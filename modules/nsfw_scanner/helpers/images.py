import asyncio
import os
import random
import traceback
import uuid
from typing import Any

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


async def process_image(
    scanner,
    original_filename: str,
    guild_id: int | None = None,
    clean_up: bool = True,
    settings: dict[str, Any] | None = None,
    accelerated: bool | None = None,
) -> dict[str, Any] | None:
    _, ext = os.path.splitext(original_filename)
    needs_conversion = ext.lower() != ".png"
    if needs_conversion:
        png_converted_path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex[:12]}.png")
        conversion_result = await asyncio.to_thread(
            convert_to_png_safe, original_filename, png_converted_path
        )
        if not conversion_result:
            print(f"[process_image] PNG conversion failed: {original_filename}")
            return None
    else:
        png_converted_path = original_filename

    try:
        with Image.open(png_converted_path) as image_file:
            image = image_file
            converted_image = None
            if image.mode != "RGBA":
                converted_image = image.convert("RGBA")
                image = converted_image
            try:
                settings_map = settings
                if settings_map is None:
                    settings_map = await mysql.get_settings(
                        guild_id,
                        [NSFW_CATEGORY_SETTING, "threshold", "nsfw-high-accuracy"],
                    )
                settings_map = settings_map or {}

                accelerated_flag = accelerated
                if accelerated_flag is None and guild_id is not None:
                    accelerated_flag = await mysql.is_accelerated(guild_id=guild_id)
                accelerated_flag = bool(accelerated_flag)

                allowed_categories = settings_map.get(NSFW_CATEGORY_SETTING) or []
                try:
                    moderation_threshold = float(settings_map.get("threshold", 0.7))
                except (TypeError, ValueError):
                    moderation_threshold = 0.7
                high_accuracy = bool(settings_map.get("nsfw-high-accuracy"))
                similarity_response = await asyncio.to_thread(
                    clip_vectors.query_similar, image, threshold=0
                )

                best_match = None
                max_similarity = 0.0
                max_category = None
                if similarity_response:
                    best_match = max(
                        similarity_response,
                        key=lambda item: float(item.get("similarity", 0) or 0),
                    )
                    max_similarity = float(best_match.get("similarity", 0) or 0)
                    max_category = best_match.get("category")

                refresh_triggered = (
                    best_match
                    and not accelerated_flag
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
                    similarity_response
                    and not refresh_triggered
                    and (not high_accuracy or max_similarity >= HIGH_ACCURACY_SIMILARITY)
                )

                if allow_similarity_shortcut:
                    for item in similarity_response:
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
                                "high_accuracy": high_accuracy,
                                "clip_threshold": CLIP_THRESHOLD,
                                "similarity": similarity,
                            }

                        if is_allowed_category(category, allowed_categories):
                            return {
                                "is_nsfw": True,
                                "category": category,
                                "reason": "similarity_match",
                                "max_similarity": max_similarity,
                                "max_category": max_category,
                                "high_accuracy": high_accuracy,
                                "clip_threshold": CLIP_THRESHOLD,
                                "similarity": similarity,
                            }

                skip_vector = (
                    max_similarity >= CLIP_THRESHOLD and not refresh_triggered
                ) or not milvus_available
                response = await moderator_api(
                    scanner,
                    image_path=png_converted_path,
                    guild_id=guild_id,
                    image=image,
                    skip_vector_add=skip_vector,
                    max_similarity=max_similarity,
                    allowed_categories=allowed_categories,
                    threshold=moderation_threshold,
                )
                if isinstance(response, dict):
                    response.setdefault("max_similarity", max_similarity)
                    response.setdefault("max_category", max_category)
                    response.setdefault("high_accuracy", high_accuracy)
                    response.setdefault("clip_threshold", CLIP_THRESHOLD)
                return response
            finally:
                if converted_image is not None:
                    converted_image.close()
    except Exception as exc:
        print(traceback.format_exc())
        print(f"[process_image] Error processing image {original_filename}: {exc}")
        return None
    finally:
        if needs_conversion:
            safe_delete(png_converted_path)
        if clean_up:
            safe_delete(original_filename)
