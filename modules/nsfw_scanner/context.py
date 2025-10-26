from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Optional

from cogs.nsfw import NSFW_CATEGORY_SETTING
from modules.config.premium_plans import PLAN_FREE, normalize_plan_name
from modules.utils import mysql

from .helpers.images import build_image_processing_context, ImageProcessingContext
from .limits import PremiumLimits, resolve_limits


@dataclass(slots=True)
class GuildScanContext:
    guild_id: int | None
    plan: str
    limits: PremiumLimits
    settings: dict[str, Any]
    tenor_allowed: bool
    nsfw_verbose: bool
    premium_status: Optional[dict[str, Any]]
    image_context: ImageProcessingContext
    head_cache: dict[str, tuple[bool, int | None]] = field(default_factory=dict)


async def _resolve_plan(guild_id: int | None) -> str:
    if guild_id is None:
        return PLAN_FREE
    try:
        plan = await mysql.resolve_guild_plan(guild_id)
    except Exception:
        plan = PLAN_FREE
    normalized = normalize_plan_name(plan, default=PLAN_FREE)
    return normalized or PLAN_FREE


async def build_guild_scan_context(guild_id: int | None) -> GuildScanContext:
    if guild_id is None:
        limits = resolve_limits(PLAN_FREE)
        image_context = await build_image_processing_context(
            None,
            settings={},
            limits=limits,
        )
        return GuildScanContext(
            guild_id=None,
            plan=PLAN_FREE,
            limits=limits,
            settings={},
            tenor_allowed=True,
            nsfw_verbose=False,
            premium_status=None,
            image_context=image_context,
        )

    settings_future = asyncio.create_task(
        mysql.get_settings(
            guild_id,
            [NSFW_CATEGORY_SETTING, "threshold", "nsfw-high-accuracy"],
        )
    )
    plan_future = asyncio.create_task(_resolve_plan(guild_id))
    tenor_future = asyncio.create_task(mysql.get_settings(guild_id, "check-tenor-gifs"))
    verbose_future = asyncio.create_task(mysql.get_settings(guild_id, "nsfw-verbose"))
    premium_future = asyncio.create_task(mysql.get_premium_status(guild_id))

    settings_value = {}
    tenor_value = True
    verbose_value = False
    premium_status = None
    plan_value = PLAN_FREE

    results = await asyncio.gather(
        settings_future,
        plan_future,
        tenor_future,
        verbose_future,
        premium_future,
        return_exceptions=True,
    )

    if not isinstance(results[0], Exception) and results[0]:
        settings_value = results[0] or {}
    if not isinstance(results[1], Exception) and results[1]:
        plan_value = results[1]
    if not isinstance(results[2], Exception):
        tenor_value = bool(results[2])
    if not isinstance(results[3], Exception):
        verbose_value = bool(results[3])
    if not isinstance(results[4], Exception):
        premium_status = results[4] or None

    limits = resolve_limits(plan_value)
    image_context = await build_image_processing_context(
        guild_id,
        settings=settings_value,
        limits=limits,
    )

    return GuildScanContext(
        guild_id=guild_id,
        plan=plan_value,
        limits=limits,
        settings=settings_value,
        tenor_allowed=tenor_value,
        nsfw_verbose=verbose_value,
        premium_status=premium_status,
        image_context=image_context,
    )


__all__ = ["GuildScanContext", "build_guild_scan_context"]
