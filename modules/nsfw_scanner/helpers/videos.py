import asyncio
import io
import os
import time
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
from ..utils.file_ops import safe_delete
from ..utils.frames import ExtractedFrame, frames_are_similar, iter_extracted_frames
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
FREE_MAX_CONCURRENCY_CAP = 3
FREE_MAX_BATCH_CAP = 4
ACCELERATED_MAX_BATCH_CAP = 16
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
        return frames_limit, min(concurrency_limit, FREE_MAX_CONCURRENCY_CAP)

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
    queue_max = max(4, max_concurrent_frames * (2 if context.accelerated else 1))
    queue: asyncio.Queue[object] = asyncio.Queue(queue_max)
    sentinel = object()

    async def _run_extraction():
        loop = asyncio.get_running_loop()

        def _produce():
            try:
                for frame_data in iter_extracted_frames(
                    original_filename,
                    frames_to_scan,
                    use_hwaccel=context.accelerated,
                    accelerated_tier=context.accelerated,
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
    media_total_frames: int | None = None
    flagged_file: Optional[discord.File] = None
    flagged_scan: dict[str, Any] | None = None
    batch: list[ExtractedFrame] = []
    batch_cap = ACCELERATED_MAX_BATCH_CAP if context.accelerated else FREE_MAX_BATCH_CAP
    batch_size = max(1, min(max_concurrent_frames, batch_cap))
    dedupe_enabled = context.accelerated
    last_signature = None
    dedupe_threshold = 0.985 if dedupe_enabled else 0.0
    last_motion_signature = None
    motion_plateau = 0
    processed_low_risk_streak = 0
    if isinstance(frames_to_scan, int) and frames_to_scan:
        low_risk_limit = max(4, min(10, frames_to_scan // 2 or 4))
    else:
        low_risk_limit = 6
    motion_flat_limit = 8 if context.accelerated else 12
    high_confidence_threshold = 0.92
    low_risk_threshold = 0.05
    flush_timeout = 0.035 if context.accelerated else 0.06
    metrics_payload: dict[str, Any] = {
        "dedupe_skipped": 0,
        "frames_submitted": 0,
        "frames_processed": 0,
        "decode_latency_ms": 0.0,
        "flush_count": 0,
        "early_exit": None,
        "bytes_downloaded": None,
    }
    try:
        metrics_payload["bytes_downloaded"] = os.path.getsize(original_filename)
    except OSError:
        metrics_payload["bytes_downloaded"] = None

    def _effective_target() -> int:
        if isinstance(frames_to_scan, int) and frames_to_scan > 0:
            return frames_to_scan
        return processed_frames

    async def _process_batch() -> bool:
        nonlocal (
            processed_frames,
            flagged_file,
            flagged_scan,
            last_signature,
            media_total_frames,
            last_motion_signature,
            motion_plateau,
            processed_low_risk_streak,
        )
        if not batch:
            return False
        metrics_payload["frames_submitted"] += len(batch)
        started = time.perf_counter()
        results = await process_image_batch(
            scanner,
            batch.copy(),
            context,
            convert_to_png=False,
        )
        metrics_payload["decode_latency_ms"] += max(
            (time.perf_counter() - started) * 1000, 0
        )
        for frame_data, scan in results:
            processed_frames += 1
            metrics_payload["frames_processed"] += 1
            frame_total = getattr(frame_data, "total_frames", None)
            if frame_total is not None:
                media_total_frames = frame_total if media_total_frames is None else max(media_total_frames, frame_total)
            if isinstance(scan, dict):
                scan.setdefault("video_frames_scanned", None)
                scan.setdefault("video_frames_target", None)
                scan.setdefault("video_frames_media_total", None)
                scan["video_frames_scanned"] = processed_frames
                scan["video_frames_target"] = _effective_target()
                if media_total_frames is not None:
                    scan["video_frames_media_total"] = media_total_frames
                numeric_scores: list[float] = []
                for key in ("confidence", "score", "probability", "nsfw_score", "max_probability"):
                    value = scan.get(key)
                    if isinstance(value, (int, float)):
                        numeric_scores.append(float(value))
                risk_score = max(numeric_scores) if numeric_scores else 0.0
                if scan.get("is_nsfw"):
                    if risk_score >= high_confidence_threshold:
                        if metrics_payload["early_exit"] is None:
                            metrics_payload["early_exit"] = "high_confidence_hit"
                    else:
                        if metrics_payload["early_exit"] is None:
                            metrics_payload["early_exit"] = "nsfw_detected"
                    flagged_file = discord.File(
                        fp=io.BytesIO(frame_data.data),
                        filename=os.path.basename(frame_data.name),
                    )
                    flagged_scan = scan
                    scan.setdefault("pipeline_metrics", {})
                    scan["pipeline_metrics"].update(metrics_payload)
                    return True
                if risk_score <= low_risk_threshold:
                    processed_low_risk_streak += 1
                    if processed_low_risk_streak >= low_risk_limit:
                        if metrics_payload["early_exit"] is None:
                            metrics_payload["early_exit"] = "low_risk_streak"
                        stop_event.set()
                else:
                    processed_low_risk_streak = 0

            signature = frame_data.signature
            if signature is not None:
                if last_motion_signature is not None and frames_are_similar(
                    last_motion_signature, signature, threshold=0.997
                ):
                    motion_plateau += 1
                    if motion_plateau >= motion_flat_limit and not stop_event.is_set():
                        if metrics_payload["early_exit"] is None:
                            metrics_payload["early_exit"] = "flat_motion"
                        stop_event.set()
                else:
                    motion_plateau = 0
                last_motion_signature = signature
        return False

    flagged = False
    try:
        while True:
            try:
                item = await asyncio.wait_for(
                    queue.get(), timeout=flush_timeout if batch else None
                )
            except asyncio.TimeoutError:
                if batch:
                    metrics_payload["flush_count"] += 1
                    flagged = await _process_batch()
                    batch.clear()
                    if flagged:
                        stop_event.set()
                        break
                continue

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
                    metrics_payload["dedupe_skipped"] += 1
                    continue
                last_signature = signature

            batch.append(frame_data)
            if len(batch) >= batch_size:
                flagged = await _process_batch()
                batch.clear()
                if flagged:
                    stop_event.set()
                    break

        if not flagged and batch:
            metrics_payload["flush_count"] += 1
            flagged = await _process_batch()
            batch.clear()
    finally:
        stop_event.set()
        await extractor_task
        safe_delete(original_filename)

    metrics_payload["frames_scanned"] = processed_frames
    metrics_payload["frames_target"] = _effective_target()
    metrics_payload["dedupe_enabled"] = bool(dedupe_enabled)
    metrics_payload["residual_low_risk_streak"] = processed_low_risk_streak
    metrics_payload["residual_motion_plateau"] = motion_plateau

    if flagged_scan is not None:
        flagged_scan.setdefault("pipeline_metrics", {}).update(metrics_payload)

    if flagged_file and flagged_scan:
        if media_total_frames is not None:
            flagged_scan.setdefault("video_frames_media_total", None)
            flagged_scan["video_frames_media_total"] = media_total_frames
        return flagged_file, flagged_scan

    safe_scan = {
        "is_nsfw": False,
        "reason": "no_nsfw_frames_detected"
        if processed_frames > 0
        else "no_frames_extracted",
        "video_frames_scanned": processed_frames,
        "video_frames_target": _effective_target(),
        "video_frames_media_total": media_total_frames,
    }
    safe_scan.setdefault("pipeline_metrics", {}).update(metrics_payload)
    return None, safe_scan
