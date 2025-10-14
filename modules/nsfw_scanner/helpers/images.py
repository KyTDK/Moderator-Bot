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
) -> dict[str, Any] | None:
    png_converted_path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex[:12]}.png")
    conversion_result = await asyncio.to_thread(
        convert_to_png_safe, original_filename, png_converted_path
    )
    if not conversion_result:
        print(f"[process_image] PNG conversion failed: {original_filename}")
        return None

    try:
        with Image.open(png_converted_path) as image:
            accelerated = False
            settings = await mysql.get_settings(
                guild_id,
                [NSFW_CATEGORY_SETTING, "threshold", "nsfw-high-accuracy"],
            )
            if guild_id is not None:
                accelerated = await mysql.is_accelerated(guild_id=guild_id)
            allowed_categories = settings.get(NSFW_CATEGORY_SETTING, [])
            high_accuracy = bool(settings.get("nsfw-high-accuracy", False))
            similarity_response = await asyncio.to_thread(clip_vectors.query_similar, image, threshold=0)

            max_similarity = 0.0
            max_category = None
            refresh_triggered = False
            if similarity_response:
                for item in similarity_response:
                    similarity = float(item.get("similarity", 0) or 0)
                    if similarity > max_similarity:
                        max_similarity = similarity
                        max_category = item.get("category")

                if not accelerated and VECTOR_REFRESH_DIVISOR > 0 and random.randint(1, VECTOR_REFRESH_DIVISOR) == 1:
                    refresh_triggered = True
                    best_item = max(
                        similarity_response,
                        key=lambda candidate: float(candidate.get("similarity", 0) or 0)
                    )
                    vector_id = best_item.get("vector_id")
                    if vector_id is not None:
                        try:
                            await clip_vectors.delete_vectors([vector_id])
                        except Exception as exc:
                            print(f"[process_image] Failed to delete vector {vector_id}: {exc}")

            milvus_available = clip_vectors.is_available()

            if similarity_response and not refresh_triggered:
                for item in similarity_response:
                    category = item.get("category")
                    similarity = float(item.get("similarity", 0) or 0)

                    if similarity < CLIP_THRESHOLD:
                        continue

                    if high_accuracy and max_similarity < HIGH_ACCURACY_SIMILARITY:
                        break

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
                        if high_accuracy and max_similarity < HIGH_ACCURACY_SIMILARITY:
                            break
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

            # Skip vector when high-accuracy is enabled and we had a strong similarity match
            # dont skip if we are refreshing the vector
            skip_vector = (max_similarity >= CLIP_THRESHOLD and not refresh_triggered) or not milvus_available
            response = await moderator_api(
                scanner,
                image_path=png_converted_path,
                guild_id=guild_id,
                image=image,
                skip_vector_add=skip_vector,
                max_similarity=max_similarity,
            )
            if isinstance(response, dict):
                response.setdefault("max_similarity", max_similarity)
                response.setdefault("max_category", max_category)
                response.setdefault("high_accuracy", high_accuracy)
                response.setdefault("clip_threshold", CLIP_THRESHOLD)
            return response
    except Exception as exc:
        print(traceback.format_exc())
        print(f"[process_image] Error processing image {original_filename}: {exc}")
        return None
    finally:
        safe_delete(png_converted_path)
        if clean_up:
            safe_delete(original_filename)
