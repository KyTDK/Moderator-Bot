import asyncio
import io
import os
from typing import Any, Optional
from threading import Event

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
from ..utils import frames_are_similar, iter_extracted_frames, safe_delete, ExtractedFrame
from .images import (
    ImageProcessingContext,
    build_image_processing_context,
    process_image_batch,
)

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
MAX_BATCH_CAP = 16


async def _resolve_video_limits(
    guild_id: int | None,
    premium_status: Optional[dict[str, Any]] = None,
) -> tuple[Optional[int], int]:
    frames_limit: Optional[int] = MAX_FRAMES_PER_VIDEO
    concurrency_limit = MAX_CONCURRENT_FRAMES

    if guild_id is None:
        return frames_limit, concurrency_limit

    premium = premium_status
    if premium is None:
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
    *,
    context: ImageProcessingContext | None = None,
    premium_status: Optional[dict[str, Any]] = None,
) -> tuple[Optional[discord.File], dict[str, Any] | None]:
    frames_to_scan, max_concurrent_frames = await _resolve_video_limits(
        guild_id,
        premium_status=premium_status,
    )

    if context is None:
        context = await build_image_processing_context(guild_id)

    stop_event = Event()
    queue: asyncio.Queue[object] = asyncio.Queue(max(4, max_concurrent_frames * 2))
    sentinel = object()

    async def _run_extraction():
        loop = asyncio.get_running_loop()

        def _produce():
            try:
                for frame_data in iter_extracted_frames(
                    original_filename,
                    frames_to_scan,
                    use_hwaccel=context.accelerated,
                    stop_event=stop_event,
                ):
                    fut = asyncio.run_coroutine_threadsafe(
                        queue.put(frame_data), loop
                    )
                    try:
                        fut.result()
                    except Exception:
                        break
                    if stop_event.is_set():
                        break
            finally:
                asyncio.run_coroutine_threadsafe(queue.put(sentinel), loop).result()

        return await asyncio.to_thread(_produce)

    extractor_task = asyncio.create_task(_run_extraction())

    processed_frames = 0
    flagged_file: Optional[discord.File] = None
    flagged_scan: dict[str, Any] | None = None
    batch: list[ExtractedFrame] = []
    batch_size = max(1, min(max_concurrent_frames, MAX_BATCH_CAP))
    dedupe_enabled = context.accelerated
    last_signature = None
    dedupe_threshold = 0.995 if dedupe_enabled else 0.0

    def _effective_target() -> int:
        if isinstance(frames_to_scan, int) and frames_to_scan > 0:
            return frames_to_scan
        return processed_frames

    async def _process_batch() -> bool:
        nonlocal processed_frames, flagged_file, flagged_scan, last_signature
        if not batch:
            return False
        results = await process_image_batch(
            scanner,
            batch.copy(),
            context,
            convert_to_png=False,
        )
        for frame_data, scan in results:
            processed_frames += 1
            if isinstance(scan, dict):
                scan.setdefault("video_frames_scanned", None)
                scan.setdefault("video_frames_target", None)
                scan["video_frames_scanned"] = processed_frames
                scan["video_frames_target"] = _effective_target()
                if scan.get("is_nsfw"):
                    flagged_file = discord.File(
                        fp=io.BytesIO(frame_data.data),
                        filename=os.path.basename(frame_data.name),
                    )
                    flagged_scan = scan
                    return True
        return False

    flagged = False
    try:
        while True:
            item = await queue.get()
            if item is sentinel:
                break
            if item is None:
                continue
            frame_data = item

            if flagged:
                continue

            if dedupe_enabled:
                signature = frame_data.signature
                if frames_are_similar(last_signature, signature, threshold=dedupe_threshold):
                    continue
                last_signature = signature

            batch.append(frame_data)
            if len(batch) >= batch_size:
                flagged = await _process_batch()
                batch.clear()
                if flagged:
                    stop_event.set()

        if not flagged and batch:
            flagged = await _process_batch()
            batch.clear()
    finally:
        stop_event.set()
        await extractor_task
        safe_delete(original_filename)

    if flagged_file and flagged_scan:
        return flagged_file, flagged_scan

    return None, {
        "is_nsfw": False,
        "reason": "no_nsfw_frames_detected"
        if processed_frames > 0
        else "no_frames_extracted",
        "video_frames_scanned": processed_frames,
        "video_frames_target": _effective_target(),
    }
