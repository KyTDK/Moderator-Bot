import asyncio
import base64
import os
from typing import Any

import openai
from PIL import Image

from cogs.nsfw import NSFW_CATEGORY_SETTING
from modules.utils import api, clip_vectors, mysql

from ..constants import ADD_SFW_VECTOR, SFW_VECTOR_MAX_SIMILARITY
from ..utils.categories import is_allowed_category
from ..utils.file_ops import file_to_b64


def _should_add_sfw_vector(
    flagged_any: bool,
    skip_vector_add: bool,
    max_similarity: float | None,
) -> bool:
    if flagged_any or skip_vector_add:
        return False
    if max_similarity is None:
        return True
    return max_similarity <= SFW_VECTOR_MAX_SIMILARITY


async def _get_moderations_resource(client):
    """
    Lazily resolve client.moderations in a thread so that the heavy OpenAI
    imports it triggers do not block the event loop when the first scan runs.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: client.moderations)


async def moderator_api(
    scanner,
    text: str | None = None,
    image_path: str | None = None,
    image: Image.Image | None = None,
    image_bytes: bytes | None = None,
    image_mime: str | None = None,
    guild_id: int | None = None,
    max_attempts: int = 3,
    skip_vector_add: bool = False,
    max_similarity: float | None = None,
    allowed_categories: list[str] | None = None,
    threshold: float | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "is_nsfw": None,
        "category": None,
        "score": 0.0,
        "reason": None,
    }

    inputs: list[Any] | str = []
    has_image_input = image_path is not None or image_bytes is not None

    if text and not has_image_input:
        inputs = text

    if has_image_input:
        b64_data: str | None = None
        if image_bytes is not None:
            try:
                b64_data = base64.b64encode(image_bytes).decode()
            except Exception as exc:
                print(f"[moderator_api] Failed to encode image bytes: {exc}")
                return result
        elif image_path is not None:
            if not os.path.exists(image_path):
                print(f"[moderator_api] Image path does not exist: {image_path}")
                return result
            try:
                b64_data = await asyncio.to_thread(file_to_b64, image_path)
            except Exception as exc:  # pragma: no cover - best effort logging
                print(f"[moderator_api] Error reading image {image_path}: {exc}")
                return result
        if not b64_data:
            print("[moderator_api] No image content was provided")
            return result
        mime_type = image_mime or "image/jpeg"
        inputs = [
            {
                "type": "image_url",
                "image_url": {"url": f"data:{mime_type};base64,{b64_data}"},
            }
        ]

    if not inputs:
        print("[moderator_api] No inputs were provided")
        return result

    resolved_allowed_categories = allowed_categories
    resolved_threshold = threshold
    settings_map: dict[str, Any] | None = None

    if guild_id is not None and (
        resolved_allowed_categories is None or resolved_threshold is None
    ):
        settings_map = await mysql.get_settings(
            guild_id, [NSFW_CATEGORY_SETTING, "threshold"]
        )

    if resolved_allowed_categories is None:
        resolved_allowed_categories = (settings_map or {}).get(
            NSFW_CATEGORY_SETTING, []
        ) or []

    if resolved_threshold is None:
        try:
            resolved_threshold = float((settings_map or {}).get("threshold", 0.7))
        except (TypeError, ValueError):
            resolved_threshold = 0.7

    if resolved_allowed_categories is None:
        resolved_allowed_categories = []
    if resolved_threshold is None:
        resolved_threshold = 0.7

    for _ in range(max_attempts):
        client, encrypted_key = await api.get_api_client(guild_id)
        if not client:
            print("[moderator_api] No available API key.")
            await asyncio.sleep(2)
            continue
        try:
            moderations_resource = await _get_moderations_resource(client)
            response = await moderations_resource.create(
                model="omni-moderation-latest" if has_image_input else "text-moderation-latest",
                input=inputs,
            )
        except openai.AuthenticationError:
            print("[moderator_api] Authentication failed. Marking key as not working.")
            await api.set_api_key_not_working(api_key=encrypted_key, bot=scanner.bot)
            continue
        except openai.RateLimitError as exc:
            print(f"[moderator_api] Rate limit error: {exc}. Marking key as not working.")
            await api.set_api_key_not_working(api_key=encrypted_key, bot=scanner.bot)
            continue
        except Exception as exc:
            print(f"[moderator_api] Unexpected error from OpenAI API: {exc}.")
            continue

        if not response or not response.results:
            print("[moderator_api] No moderation results returned.")
            continue

        if not await api.is_api_key_working(encrypted_key):
            await api.set_api_key_working(encrypted_key)

        results = response.results[0]
        guild_flagged_categories: list[tuple[str, float]] = []
        summary_categories = {} # category: score
        flagged_any = False
        for category, is_flagged in results.categories.__dict__.items():
            normalized_category = category.replace("/", "_").replace("-", "_")
            score = results.category_scores.__dict__.get(category, 0)

            if is_flagged:
                flagged_any = True

            summary_categories[normalized_category] = score

            if is_flagged and not skip_vector_add and clip_vectors.is_available():
                await asyncio.to_thread(
                    clip_vectors.add_vector,
                    image,
                    metadata={"category": normalized_category, "score": score},
                )

            if score < resolved_threshold:
                continue

            if resolved_allowed_categories and not is_allowed_category(
                category, resolved_allowed_categories
            ):
                continue

            guild_flagged_categories.append((normalized_category, score))

        if (
            ADD_SFW_VECTOR
            and image is not None
            and clip_vectors.is_available()
            and _should_add_sfw_vector(flagged_any, skip_vector_add, max_similarity)
        ):
            await asyncio.to_thread(
                clip_vectors.add_vector,
                image,
                metadata={"category": None, "score": 0},
            )

        if guild_flagged_categories:
            guild_flagged_categories.sort(key=lambda item: item[1], reverse=True)
            best_category, best_score = guild_flagged_categories[0]
            return {
                "is_nsfw": True,
                "category": best_category,
                "score": best_score,
                "reason": "openai_moderation",
                "threshold": resolved_threshold,
                "summary_categories": summary_categories,
            }

        return {
            "is_nsfw": False,
            "reason": "openai_moderation",
            "flagged_any": flagged_any,
            "threshold": resolved_threshold,
            "summary_categories": summary_categories,
        }

    return result
