import asyncio
import os
from typing import Any

import openai
from PIL import Image

from cogs.nsfw import NSFW_CATEGORY_SETTING
from modules.utils import api, clip_vectors, mysql

from ..constants import ADD_SFW_VECTOR
from ..utils import file_to_b64, is_allowed_category

async def moderator_api(
    scanner,
    text: str | None = None,
    image_path: str | None = None,
    image: Image.Image | None = None,
    guild_id: int | None = None,
    max_attempts: int = 3,
    skip_vector_add: bool = False,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "is_nsfw": None,
        "category": None,
        "score": 0.0,
        "reason": None,
    }

    inputs: list[Any] | str = []
    is_video = image_path is not None

    if text and not image_path:
        inputs = text

    if is_video:
        if not os.path.exists(image_path):
            print(f"[moderator_api] Image path does not exist: {image_path}")
            return result
        try:
            b64 = await asyncio.to_thread(file_to_b64, image_path)
        except Exception as exc:  # pragma: no cover - best effort logging
            print(f"[moderator_api] Error reading/encoding image {image_path}: {exc}")
            return result
        inputs.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
            }
        )

    if not inputs:
        print("[moderator_api] No inputs were provided")
        return result

    for _ in range(max_attempts):
        client, encrypted_key = await api.get_api_client(guild_id)
        if not client:
            print("[moderator_api] No available API key.")
            await asyncio.sleep(2)
            continue
        try:
            response = await client.moderations.create(
                model="omni-moderation-latest" if image_path else "text-moderation-latest",
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
        settings = await mysql.get_settings(guild_id, [NSFW_CATEGORY_SETTING, "threshold"])
        allowed_categories = settings.get(NSFW_CATEGORY_SETTING, [])
        try:
            threshold = float(settings.get("threshold", 0.7))
        except (TypeError, ValueError):
            threshold = 0.7
        guild_flagged_categories: list[tuple[str, float]] = []
        summary_categories = {} # category: score
        flagged_any = False
        for category, is_flagged in results.categories.__dict__.items():
            normalized_category = category.replace("/", "_").replace("-", "_")
            score = results.category_scores.__dict__.get(category, 0)
            if not is_flagged:
                continue
            else:
                flagged_any = True
            
            summary_categories[normalized_category] = score

            if not skip_vector_add:
                await asyncio.to_thread(clip_vectors.add_vector, image, metadata={"category": normalized_category, "score": score})

            if score < threshold:
                continue

            if allowed_categories and not is_allowed_category(category, allowed_categories):
                continue

            guild_flagged_categories.append((normalized_category, score))

        if ADD_SFW_VECTOR and not flagged_any and not skip_vector_add:
            await asyncio.to_thread(clip_vectors.add_vector, image, metadata={"category": None, "score": 0})

        if guild_flagged_categories:
            guild_flagged_categories.sort(key=lambda item: item[1], reverse=True)
            best_category, best_score = guild_flagged_categories[0]
            return {
                "is_nsfw": True,
                "category": best_category,
                "score": best_score,
                "reason": "openai_moderation",
                "threshold": threshold,
                "summary_categories": summary_categories,
            }

        return {
            "is_nsfw": False,
            "reason": "openai_moderation",
            "flagged_any": flagged_any,
            "threshold": threshold,
            "summary_categories": summary_categories,
        }

    return result
