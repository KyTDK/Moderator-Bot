from __future__ import annotations

from typing import Any, Mapping

from modules.utils import mysql

from ..helpers.attachments import AttachmentSettingsCache


async def resolve_download_cap_bytes(
    guild_id: int | None,
    settings_cache: AttachmentSettingsCache,
    plan_caps: Mapping[str, int | None],
    default_download_cap: int | None,
) -> int | None:
    if guild_id is None:
        return default_download_cap

    if settings_cache.has_premium_plan():
        plan = settings_cache.get_premium_plan()
    else:
        plan = None
        try:
            plan = await mysql.resolve_guild_plan(guild_id)
        except Exception:
            plan = None
        settings_cache.set_premium_plan(plan)

    normalized_plan = (plan or "free").lower()
    return plan_caps.get(normalized_plan, default_download_cap)


async def resolve_settings_map(
    guild_id: int | None,
    settings_cache: AttachmentSettingsCache,
) -> dict[str, Any]:
    cached = settings_cache.get_scan_settings()
    if cached is not None:
        return cached

    if guild_id is None:
        return {}

    try:
        settings_map = await mysql.get_settings(guild_id)
    except Exception:
        settings_map = {}
    settings_cache.set_scan_settings(settings_map)
    return settings_cache.get_scan_settings() or {}
