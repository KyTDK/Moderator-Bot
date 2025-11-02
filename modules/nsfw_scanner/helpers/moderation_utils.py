from __future__ import annotations

import os
from typing import Any

from modules.nsfw_scanner.settings_keys import (
    NSFW_IMAGE_CATEGORY_SETTING,
    NSFW_TEXT_CATEGORY_SETTING,
    NSFW_TEXT_ENABLED_SETTING,
    NSFW_TEXT_THRESHOLD_SETTING,
    NSFW_THRESHOLD_SETTING,
)
from modules.utils import mysql

__all__ = [
    "ALLOW_REMOTE_IMAGES",
    "REMOTE_ALLOWED_HOSTS",
    "REMOTE_MIN_BYTES",
    "should_use_remote_source",
    "should_add_sfw_vector",
    "resolve_moderation_settings",
]


ALLOW_REMOTE_IMAGES = os.getenv("MODBOT_ENABLE_REMOTE_IMAGE_URLS", "1").lower() not in {
    "0",
    "false",
    "no",
    "off",
}
REMOTE_ALLOWED_HOSTS = {
    "cdn.discordapp.com",
    "media.discordapp.net",
}
REMOTE_MIN_BYTES = int(os.getenv("MODBOT_MODERATION_REMOTE_MIN_BYTES", "524288"))


def should_use_remote_source(
    source_url: str | None,
    *,
    payload_size: int | None,
) -> bool:
    if not ALLOW_REMOTE_IMAGES or not source_url:
        return False
    try:
        from urllib.parse import urlparse

        parsed = urlparse(source_url)
    except Exception:
        return False
    if parsed.scheme not in {"https"}:
        return False
    if parsed.hostname not in REMOTE_ALLOWED_HOSTS:
        return False
    if payload_size is not None and payload_size < REMOTE_MIN_BYTES:
        return False
    return True


def should_add_sfw_vector(
    flagged_any: bool,
    skip_vector_add: bool,
    max_similarity: float | None,
) -> bool:
    from ..constants import SFW_VECTOR_MAX_SIMILARITY

    if flagged_any or skip_vector_add:
        return False
    if max_similarity is None:
        return True
    return max_similarity <= SFW_VECTOR_MAX_SIMILARITY


async def resolve_moderation_settings(
    *,
    guild_id: int | None,
    use_text_settings: bool,
    allowed_categories: list[str] | None,
    threshold: float | None,
) -> tuple[list[str], float]:
    settings_map: dict[str, Any] | None = None

    need_settings = False
    if guild_id is not None:
        need_categories = allowed_categories is None
        need_threshold = threshold is None
        if need_categories or need_threshold:
            need_settings = True

    if need_settings:
        requested_settings = [NSFW_IMAGE_CATEGORY_SETTING, NSFW_THRESHOLD_SETTING]
        if use_text_settings:
            requested_settings.extend(
                [NSFW_TEXT_CATEGORY_SETTING, NSFW_TEXT_THRESHOLD_SETTING, NSFW_TEXT_ENABLED_SETTING]
            )
        settings_map = await mysql.get_settings(guild_id, requested_settings)

    settings_source = settings_map or {}

    if allowed_categories is None:
        resolved_categories = settings_source.get(NSFW_TEXT_CATEGORY_SETTING) if use_text_settings else None
        if not resolved_categories:
            resolved_categories = settings_source.get(NSFW_IMAGE_CATEGORY_SETTING, [])
    else:
        resolved_categories = allowed_categories

    if threshold is None:
        threshold_value = (
            settings_source.get(NSFW_TEXT_THRESHOLD_SETTING)
            if use_text_settings
            else None
        )
        if threshold_value is None:
            threshold_value = settings_source.get(NSFW_THRESHOLD_SETTING, 0.7)
        try:
            resolved_threshold = float(threshold_value)
        except (TypeError, ValueError):
            resolved_threshold = 0.7
    else:
        resolved_threshold = threshold

    if resolved_categories is None:
        resolved_categories = []
    if resolved_threshold is None:
        resolved_threshold = 0.7

    return list(resolved_categories), float(resolved_threshold)
