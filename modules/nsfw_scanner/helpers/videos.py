import asyncio
import io
import os
import time
from dataclasses import dataclass, field
from threading import Event
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
    ACCELERATED_VIDEO_BATCH_CAP,
    FREE_VIDEO_BATCH_CAP,
    FREE_VIDEO_MAX_CONCURRENCY_CAP,
    MAX_CONCURRENT_FRAMES,
    MAX_FRAMES_PER_VIDEO,
    VIDEO_SCAN_WALL_CLOCK_LIMIT_SECONDS,
    VIDEO_MAX_BATCH_CAP,
)
from ..utils.file_ops import safe_delete
from ..utils.frames import ExtractedFrame, frames_are_similar, iter_extracted_frames
from ..utils.frames.ffmpeg import ffmpeg_available
from .context import ImageProcessingContext, build_image_processing_context
from .images import process_image_batch
from .metrics import LatencyTracker


def _initial_video_metrics() -> dict[str, Any]:
    return {
        "dedupe_skipped": 0,
        "frames_submitted": 0,
        "frames_processed": 0,
        "decode_latency_ms": 0.0,
        "batch_decode_latency_ms": 0.0,
        "batch_similarity_latency_ms": 0.0,
        "moderation_latency_ms": 0.0,
        "moderation_wait_latency_ms": 0.0,
        "queue_wait_latency_ms": 0.0,
        "dedupe_check_latency_ms": 0.0,
        "extraction_latency_ms": 0.0,
        "flush_count": 0,
        "early_exit": None,
        "bytes_downloaded": None,
        "avg_frame_interarrival_ms": 0.0,
        "effective_flush_timeout_ms": 0.0,
    }


@dataclass(slots=True)
class VideoMetrics:
    tracker: LatencyTracker
    data: dict[str, Any] = field(default_factory=_initial_video_metrics)

    def snapshot(self) -> dict[str, Any]:
        return dict(self.data)

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def increment(self, key: str, amount: int = 1) -> None:
        self.data[key] = int(self.data.get(key) or 0) + int(amount)

    def add_duration(self, key: str, duration_ms: Any) -> None:
        try:
            duration = float(duration_ms)
        except (TypeError, ValueError):
            return
        if duration <= 0:
            return
        self.data[key] = float(self.data.get(key) or 0.0) + duration

    def set_bytes_downloaded(self, value: Any) -> None:
        self.data["bytes_downloaded"] = value

    def note_early_exit(self, reason: str) -> None:
        if self.data.get("early_exit") is None:
            self.data["early_exit"] = reason

    def has_early_exit(self) -> bool:
        return self.data.get("early_exit") is not None

    def finalize(
        self,
        *,
        processed_frames: int,
        target_frames: int,
        dedupe_enabled: bool,
        low_risk_streak: int,
        motion_plateau: int,
        avg_interarrival: float | None,
        min_flush_timeout: float,
        max_flush_timeout: float,
    ) -> dict[str, Any]:
        payload = self.data
        payload["frames_scanned"] = processed_frames
        payload["frames_target"] = target_frames
        payload["dedupe_enabled"] = bool(dedupe_enabled)
        payload["residual_low_risk_streak"] = low_risk_streak
        payload["residual_motion_plateau"] = motion_plateau

        if avg_interarrival is not None:
            payload["avg_frame_interarrival_ms"] = avg_interarrival * 1000
            payload["effective_flush_timeout_ms"] = max(
                min_flush_timeout,
                min(max_flush_timeout, avg_interarrival * 1.5),
            ) * 1000
        else:
            payload["effective_flush_timeout_ms"] = min_flush_timeout * 1000

        total_duration_ms = self.tracker.total_duration_ms()
        payload["total_latency_ms"] = total_duration_ms
        payload["latency_breakdown_ms"] = {
            key: value
            for key, value in self.tracker.steps.items()
            if isinstance(value, dict)
        }
        return payload

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
FREE_MAX_CONCURRENCY_CAP = max(1, FREE_VIDEO_MAX_CONCURRENCY_CAP or 4)
FREE_MAX_BATCH_CAP = max(1, FREE_VIDEO_BATCH_CAP or 4)
ACCELERATED_MAX_BATCH_CAP = max(4, ACCELERATED_VIDEO_BATCH_CAP or 16)
MAX_BATCH_CAP = max(4, VIDEO_MAX_BATCH_CAP or 16)


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
    payload_metadata: dict[str, Any] | None = None,
) -> tuple[Optional[discord.File], dict[str, Any] | None]:
    overall_started = time.perf_counter()
    frames_to_scan, max_concurrent_frames = await _resolve_video_limits(
        guild_id,
        premium_status=premium_status,
    )
    cpu_count = max(os.cpu_count() or 1, 1)
    if isinstance(max_concurrent_frames, int) and max_concurrent_frames > 0:
        max_concurrent_frames = max(1, min(max_concurrent_frames, cpu_count * 2))

    if context is None:
        context = await build_image_processing_context(guild_id)

    payload_metadata = dict(payload_metadata or {})
    payload_metadata.setdefault("guild_id", guild_id)

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

    extraction_started = time.perf_counter()
    extractor_task = asyncio.create_task(_run_extraction())

    processed_frames = 0
    media_total_frames: int | None = None
    flagged_file: Optional[discord.File] = None
    flagged_scan: dict[str, Any] | None = None
    latency_tracker = LatencyTracker()
    metrics = VideoMetrics(latency_tracker)
    batch: list[ExtractedFrame] = []
    batch_cap = ACCELERATED_MAX_BATCH_CAP if context.accelerated else FREE_MAX_BATCH_CAP
    batch_size = max(1, min(max_concurrent_frames, batch_cap))
    dedupe_enabled = True
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
    min_flush_timeout = 0.035 if context.accelerated else 0.06
    max_flush_timeout = 0.24 if context.accelerated else 0.32
    avg_interarrival: float | None = None
    last_frame_timestamp: float | None = None
    metrics.data.update(
        {
            "ffmpeg_available": ffmpeg_available(),
            "cpu_count": cpu_count,
            "frames_to_scan": frames_to_scan,
            "queue_max": queue_max,
            "queue_name": context.queue_name,
            "accelerated": context.accelerated,
            "max_concurrent_frames": max_concurrent_frames,
            "batch_size": batch_size,
            "dedupe_enabled": dedupe_enabled,
            "dedupe_threshold": dedupe_threshold,
            "use_hwaccel": context.accelerated,
        }
    )

    def _record_latency(name: str, duration_ms: float, label: str) -> None:
        latency_tracker.record_step(name, duration_ms, label=label)
    try:
        metrics.set_bytes_downloaded(os.path.getsize(original_filename))
    except OSError:
        metrics.set_bytes_downloaded(None)

    def _effective_target() -> int:
        if isinstance(frames_to_scan, int) and frames_to_scan > 0:
            return frames_to_scan
        return processed_frames

    async def _process_batch() -> bool:
        nonlocal processed_frames, flagged_file, flagged_scan, last_signature
        nonlocal media_total_frames, last_motion_signature
        nonlocal motion_plateau, processed_low_risk_streak
        if not batch:
            return False
        metrics.increment("frames_submitted", len(batch))
        results, batch_metrics = await process_image_batch(
            scanner,
            batch.copy(),
            context,
            convert_to_png=False,
            max_concurrent_frames=max_concurrent_frames,
            payload_metadata=payload_metadata,
        )
        decode_ms = float(batch_metrics.get("decode_latency_ms") or 0.0)
        metrics.add_duration("decode_latency_ms", decode_ms)
        metrics.add_duration("batch_decode_latency_ms", decode_ms)
        metrics.add_duration(
            "batch_similarity_latency_ms",
            batch_metrics.get("similarity_latency_ms") or 0.0
        )
        metrics.add_duration(
            "moderation_latency_ms",
            batch_metrics.get("moderation_latency_ms") or 0.0
        )
        metrics.add_duration(
            "moderation_wait_latency_ms",
            batch_metrics.get("moderation_wait_latency_ms") or 0.0
        )
        for frame_data, scan in results:
            processed_frames += 1
            metrics.increment("frames_processed")
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
                        if not metrics.has_early_exit():
                            metrics.note_early_exit("high_confidence_hit")
                    else:
                        if not metrics.has_early_exit():
                            metrics.note_early_exit("nsfw_detected")
                    flagged_file = discord.File(
                        fp=io.BytesIO(frame_data.data),
                        filename=os.path.basename(frame_data.name),
                    )
                    flagged_scan = scan
                    scan.setdefault("pipeline_metrics", {})
                    scan["pipeline_metrics"].update(metrics.snapshot())
                    return True
                if risk_score <= low_risk_threshold:
                    processed_low_risk_streak += 1
                    if processed_low_risk_streak >= low_risk_limit:
                        if not metrics.has_early_exit():
                            metrics.note_early_exit("low_risk_streak")
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
                        if not metrics.has_early_exit():
                            metrics.note_early_exit("flat_motion")
                        stop_event.set()
                else:
                    motion_plateau = 0
                last_motion_signature = signature
        return False

    flagged = False
    try:
        while True:
            if (
                VIDEO_SCAN_WALL_CLOCK_LIMIT_SECONDS
                and (time.perf_counter() - overall_started) >= VIDEO_SCAN_WALL_CLOCK_LIMIT_SECONDS
            ):
                if not metrics.has_early_exit():
                    metrics.note_early_exit("wall_clock_timeout")
                stop_event.set()
                break
            try:
                wait_started = time.perf_counter()
                timeout: float | None = None
                if batch:
                    if avg_interarrival is not None:
                        timeout = max(
                            min_flush_timeout,
                            min(max_flush_timeout, avg_interarrival * 1.5),
                        )
                    else:
                        timeout = min_flush_timeout
                item = await asyncio.wait_for(queue.get(), timeout=timeout)
                metrics.add_duration(
                    "queue_wait_latency_ms",
                    (time.perf_counter() - wait_started) * 1000,
                )
            except asyncio.TimeoutError:
                metrics.add_duration(
                    "queue_wait_latency_ms",
                    (time.perf_counter() - wait_started) * 1000,
                )
                if batch:
                    metrics.increment("flush_count")
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
            now = time.perf_counter()
            if last_frame_timestamp is not None:
                inter_arrival = max(now - last_frame_timestamp, 0.0)
                capped = min(inter_arrival, max_flush_timeout)
                if avg_interarrival is None:
                    avg_interarrival = capped
                else:
                    avg_interarrival = (avg_interarrival * 0.7) + (capped * 0.3)
            last_frame_timestamp = now

            if flagged:
                continue

            if dedupe_enabled:
                dedupe_started = time.perf_counter()
                signature = frame_data.signature
                if frames_are_similar(last_signature, signature, threshold=dedupe_threshold):
                    metrics.increment("dedupe_skipped")
                    metrics.add_duration(
                        "dedupe_check_latency_ms",
                        (time.perf_counter() - dedupe_started) * 1000,
                    )
                    continue
                last_signature = signature
                metrics.add_duration(
                    "dedupe_check_latency_ms",
                    (time.perf_counter() - dedupe_started) * 1000,
                )

            batch.append(frame_data)
            if len(batch) >= batch_size:
                flagged = await _process_batch()
                batch.clear()
                if flagged:
                    stop_event.set()
                    break

        if not flagged and batch:
            metrics.increment("flush_count")
            flagged = await _process_batch()
            batch.clear()
    finally:
        stop_event.set()
        await extractor_task
        metrics.add_duration(
            "extraction_latency_ms",
            (time.perf_counter() - extraction_started) * 1000,
        )
        safe_delete(original_filename)

    total_duration_ms = latency_tracker.total_duration_ms()

    _record_latency(
        "frame_decode",
        float(metrics.get("batch_decode_latency_ms") or 0.0),
        "Frame Decode",
    )
    _record_latency(
        "frame_similarity",
        float(metrics.get("batch_similarity_latency_ms") or 0.0),
        "Frame Similarity Search",
    )
    _record_latency(
        "frame_moderation",
        float(metrics.get("moderation_latency_ms") or 0.0),
        "Frame Moderator Calls",
    )
    _record_latency(
        "frame_moderation_wait",
        float(metrics.get("moderation_wait_latency_ms") or 0.0),
        "Moderator Queue Wait",
    )
    _record_latency(
        "frame_dedupe",
        float(metrics.get("dedupe_check_latency_ms") or 0.0),
        "Dedupe Checks",
    )
    _record_latency(
        "frame_extraction",
        float(metrics.get("extraction_latency_ms") or 0.0),
        "Frame Extraction",
    )
    _record_latency(
        "frame_queue_wait",
        float(metrics.get("queue_wait_latency_ms") or 0.0),
        "Frame Queue Wait",
    )

    frame_pipeline_ms = (
        float(metrics.get("batch_decode_latency_ms") or 0.0)
        + float(metrics.get("batch_similarity_latency_ms") or 0.0)
        + float(metrics.get("moderation_latency_ms") or 0.0)
        + float(metrics.get("moderation_wait_latency_ms") or 0.0)
        + float(metrics.get("dedupe_check_latency_ms") or 0.0)
    )
    if frame_pipeline_ms > 0:
        _record_latency("frame_pipeline", frame_pipeline_ms, "Frame Pipeline")

    overhead_ms = max(
        total_duration_ms
        - frame_pipeline_ms
        - float(metrics.get("queue_wait_latency_ms") or 0.0)
        - float(metrics.get("extraction_latency_ms") or 0.0),
        0.0,
    )
    if overhead_ms > 0:
        _record_latency("coordination", overhead_ms, "Coordinator Overhead")

    target_frames = _effective_target()
    metrics_payload = metrics.finalize(
        processed_frames=processed_frames,
        target_frames=target_frames,
        dedupe_enabled=dedupe_enabled,
        low_risk_streak=processed_low_risk_streak,
        motion_plateau=motion_plateau,
        avg_interarrival=avg_interarrival,
        min_flush_timeout=min_flush_timeout,
        max_flush_timeout=max_flush_timeout,
    )

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
        "video_frames_target": target_frames,
        "video_frames_media_total": media_total_frames,
    }
    safe_scan.setdefault("pipeline_metrics", {}).update(metrics_payload)
    return None, safe_scan
