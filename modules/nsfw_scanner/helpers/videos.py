import asyncio
import os
from typing import Any, Optional

import discord

from modules.utils import mysql

from ..constants import (
    ACCELERATED_MAX_CONCURRENT_FRAMES,
    ACCELERATED_MAX_FRAMES_PER_VIDEO,
    MAX_CONCURRENT_FRAMES,
    MAX_FRAMES_PER_VIDEO,
)
from ..utils import extract_frames_threaded, safe_delete
from .images import process_image


async def process_video(
    scanner,
    original_filename: str,
    guild_id: int,
) -> tuple[Optional[discord.File], dict[str, Any] | None]:
    frames_to_scan = MAX_FRAMES_PER_VIDEO
    if await mysql.is_accelerated(guild_id=guild_id):
        frames_to_scan = ACCELERATED_MAX_FRAMES_PER_VIDEO

    temp_frames = await asyncio.to_thread(
        extract_frames_threaded, original_filename, frames_to_scan
    )
    print(
        f"[process_video] extracted {len(temp_frames)} frames (target={frames_to_scan})"
    )
    if not temp_frames:
        safe_delete(original_filename)
        return None, {
            "is_nsfw": False,
            "reason": "No frames extracted",
            "video_frames_scanned": 0,
            "video_frames_target": frames_to_scan,
        }

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_FRAMES)
    if await mysql.is_accelerated(guild_id=guild_id):
        semaphore = asyncio.Semaphore(ACCELERATED_MAX_CONCURRENT_FRAMES)

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
                category_name = scan.get("category") or "unspecified"
            else:
                category_name = str(scan)

            print(
                f"[process_video] NSFW detected in frame: {frame_path} (category: {category_name})"
            )
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            flagged_file = discord.File(
                frame_path, filename=os.path.basename(frame_path)
            )
            return flagged_file, scan

        return None, {
            "is_nsfw": False,
            "reason": "No NSFW frames detected",
            "video_frames_scanned": len(temp_frames),
            "video_frames_target": frames_to_scan,
        }
    finally:
        for frame in temp_frames:
            safe_delete(frame)
        safe_delete(original_filename)
