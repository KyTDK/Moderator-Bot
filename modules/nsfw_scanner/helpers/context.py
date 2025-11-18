from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from modules.nsfw_scanner.settings_keys import (
    NSFW_HIGH_ACCURACY_SETTING,
    NSFW_IMAGE_CATEGORY_SETTING,
    NSFW_OCR_LANGUAGES_SETTING,
    NSFW_TEXT_CATEGORY_SETTING,
    NSFW_TEXT_ENABLED_SETTING,
    NSFW_TEXT_EXCLUDED_CHANNELS_SETTING,
    NSFW_TEXT_STRIKES_ONLY_SETTING,
    NSFW_TEXT_THRESHOLD_SETTING,
    NSFW_THRESHOLD_SETTING,
)
from modules.utils import mysql


@dataclass(slots=True)
class ImageProcessingContext:
    guild_id: int | None
    settings_map: dict[str, Any]
    allowed_categories: list[str]
    moderation_threshold: float
    text_allowed_categories: list[str]
    text_moderation_threshold: float
    high_accuracy: bool
    accelerated: bool
    queue_name: str | None = None


async def build_image_processing_context(
    guild_id: int | None,
    settings: dict[str, Any] | None = None,
    accelerated: bool | None = None,
    queue_name: str | None = None,
) -> ImageProcessingContext:
    """Build a shared image processing context for scans."""
    settings_map: dict[str, Any] = settings.copy() if settings else {}

    if not settings_map and guild_id is not None:
        settings_map = await mysql.get_settings(
            guild_id,
            [
                NSFW_IMAGE_CATEGORY_SETTING,
                NSFW_TEXT_CATEGORY_SETTING,
                NSFW_THRESHOLD_SETTING,
                NSFW_TEXT_THRESHOLD_SETTING,
                NSFW_HIGH_ACCURACY_SETTING,
                NSFW_TEXT_ENABLED_SETTING,
                NSFW_TEXT_EXCLUDED_CHANNELS_SETTING,
                NSFW_TEXT_STRIKES_ONLY_SETTING,
                NSFW_OCR_LANGUAGES_SETTING,
            ],
        ) or {}

    try:
        moderation_threshold = float(settings_map.get(NSFW_THRESHOLD_SETTING, 0.7))
    except (TypeError, ValueError):
        moderation_threshold = 0.7

    high_accuracy = bool(settings_map.get(NSFW_HIGH_ACCURACY_SETTING))
    allowed_categories = list(settings_map.get(NSFW_IMAGE_CATEGORY_SETTING) or [])
    text_allowed_categories = list(
        settings_map.get(NSFW_TEXT_CATEGORY_SETTING) or allowed_categories
    )

    accelerated_flag = bool(accelerated)
    if accelerated_flag is False and accelerated is None and guild_id is not None:
        try:
            accelerated_flag = bool(await mysql.is_accelerated(guild_id=guild_id))
        except Exception:
            accelerated_flag = False

    try:
        text_moderation_threshold = float(
            settings_map.get(NSFW_TEXT_THRESHOLD_SETTING, moderation_threshold)
        )
    except (TypeError, ValueError):
        text_moderation_threshold = moderation_threshold

    return ImageProcessingContext(
        guild_id=guild_id,
        settings_map=settings_map,
        allowed_categories=allowed_categories,
        moderation_threshold=moderation_threshold,
        text_allowed_categories=text_allowed_categories,
        text_moderation_threshold=text_moderation_threshold,
        high_accuracy=high_accuracy,
        accelerated=accelerated_flag,
        queue_name=queue_name,
    )
