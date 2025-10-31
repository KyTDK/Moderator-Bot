from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cogs.nsfw import NSFW_CATEGORY_SETTING
from modules.utils import mysql


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
    """Build a shared image processing context for scans."""
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
    allowed_categories = list(settings_map.get(NSFW_CATEGORY_SETTING) or [])

    accelerated_flag = bool(accelerated)
    if accelerated_flag is False and accelerated is None and guild_id is not None:
        try:
            accelerated_flag = bool(await mysql.is_accelerated(guild_id=guild_id))
        except Exception:
            accelerated_flag = False

    return ImageProcessingContext(
        guild_id=guild_id,
        settings_map=settings_map,
        allowed_categories=allowed_categories,
        moderation_threshold=moderation_threshold,
        high_accuracy=high_accuracy,
        accelerated=accelerated_flag,
    )
