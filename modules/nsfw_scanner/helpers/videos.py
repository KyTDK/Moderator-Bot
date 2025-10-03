import asyncio
import os
from typing import Any, Optional

import discord

from modules.utils import mysql

from ..constants import (
    ACCELERATED_MAX_CONCURRENT_FRAMES,
    ACCELERATED_MAX_FRAMES_PER_VIDEO,
    ACCELERATED_PRO_CONCURRENT_FRAMES,
    ACCELERATED_PRO_MAX_FRAMES_PER_VIDEO,
    ACCELERATED_ULTRA_CONCURRENT_FRAMES,
    ACCELERATED_ULTRA_MAX_FRAMES_PER_VIDEO,
    MAX_CONCURRENT_FRAMES,
    MAX_FRAMES_PER_VIDEO,
)
from ..utils import extract_frames_threaded, safe_delete
from .images import process_image

FRAME_LIMITS_BY_TIER = {
    "accelerated": ACCELERATED_MAX_FRAMES_PER_VIDEO,
    "accelerated_pro": ACCELERATED_PRO_MAX_FRAMES_PER_VIDEO,
    "accelerated_ultra": ACCELERATED_ULTRA_MAX_FRAMES_PER_VIDEO,
}

CONCURRENCY_LIMITS_BY_TIER = {
    "accelerated": ACCELERATED_MAX_CONCURRENT_FRAMES,
    "accelerated_pro": ACCELERATED_PRO_CONCURRENT_FRAMES,
    "accelerated_ultra": ACCELERATED_ULTRA_CONCURRENT_FRAMES,
}

DEFAULT_PREMIUM_TIER = "accelerated"


async def _resolve_video_limits(guild_id: int | None) -> tuple[Optional[int], int]:
    frames_limit: Optional[int] = MAX_FRAMES_PER_VIDEO
    concurrency_limit = MAX_CONCURRENT_FRAMES

    if guild_id is None:
        return frames_limit, concurrency_limit

    premium = await mysql.get_premium_status(guild_id)
    if not premium or not premium.get("is_active"):
        return frames_limit, concurrency_limit

    tier = premium.get("tier") or DEFAULT_PREMIUM_TIER
    frames_limit = FRAME_LIMITS_BY_TIER.get(
        tier,
        FRAME_LIMITS_BY_TIER[DEFAULT_PREMIUM_TIER],
    )
    concurrency_limit = CONCURRENCY_LIMITS_BY_TIER.get(
        tier,
        CONCURRENCY_LIMITS_BY_TIER[DEFAULT_PREMIUM_TIER],
    )
    return frames_limit, concurrency_limit


async def process_video(
    scanner,
    original_filename: str,
    guild_id: int,
) -> tuple[Optional[discord.File], dict[str, Any] | None]:
    frames_to_scan, max_concurrent_frames = await _resolve_video_limits(guild_id)
    temp_frames = await asyncio.to_thread(
        extract_frames_threaded, original_filename, frames_to_scan
    )
    if not temp_frames:
        safe_delete(original_filename)
        return None, {
            "is_nsfw": False,
            "reason": "no_frames_extracted",
            "video_frames_scanned": 0,
            "video_frames_target": frames_to_scan,
        }

    semaphore = asyncio.Semaphore(max_concurrent_frames)

    async def analyse(path: str):
        async with semaphore:
            try:
                scan = await process_image(
                    scanner,
                    original_filename=path,
                    guild_id=guild_id,
                    clean_up=False,
                )
                if isinstance(scan, dict):
                    scan.setdefault("video_frames_scanned", None)
                    scan.setdefault("video_frames_target", None)
                    scan["video_frames_scanned"] = len(temp_frames)
                    scan["video_frames_target"] = frames_to_scan
                    return (path, scan)
                return None
            except Exception as exc:
                print(f"[process_video] Analyse error {path}: {exc}")
                return None

    tasks = [asyncio.create_task(analyse(frame)) for frame in temp_frames]
    try:
        for done in asyncio.as_completed(tasks):
            res = await done
            if not res:
                continue
            frame_path, scan = res
            if isinstance(scan, dict):
                if not scan.get("is_nsfw"):
                    continue
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            flagged_file = discord.File(
                frame_path, filename=os.path.basename(frame_path)
            )
            return flagged_file, scan

        return None, {
            "is_nsfw": False,
            "reason": "no_nsfw_frames_detected",
            "video_frames_scanned": len(temp_frames),
            "video_frames_target": frames_to_scan,
        }
    finally:
        for frame in temp_frames:
            safe_delete(frame)
        safe_delete(original_filename)
