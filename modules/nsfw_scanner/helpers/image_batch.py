import asyncio
import logging
import os
import time
from typing import Any, List, Optional, Sequence, Tuple

from PIL import Image

from modules.nsfw_scanner.constants import (
    ACCELERATED_MOD_API_MAX_CONCURRENCY,
    MOD_API_MAX_CONCURRENCY,
    SIMILARITY_SEARCH_TIMEOUT_SECONDS,
)
from modules.utils import clip_vectors

from ..utils.frames import ExtractedFrame
from .context import ImageProcessingContext
from .image_io import _encode_image_to_png_bytes, _open_image_from_bytes
from .image_pipeline import _run_image_pipeline

log = logging.getLogger(__name__)


async def process_image_batch(
    scanner,
    frame_payloads: Sequence[ExtractedFrame],
    context: ImageProcessingContext,
    *,
    convert_to_png: bool = False,
    max_concurrent_frames: int | None = None,
    payload_metadata: dict[str, Any] | None = None,
) -> Tuple[
    List[Tuple[ExtractedFrame, dict[str, Any] | None]],
    dict[str, float],
]:
    """
    Analyse a batch of in-memory frames using shared settings/context.
    Returns list of (frame_data, result_dict).
    """
    prepared: list[tuple[ExtractedFrame, Image.Image | None]] = []
    valid_images: list[Image.Image] = []

    decode_tasks: list[asyncio.Task[Image.Image | None]] = []
    batch_metrics: dict[str, float] = {
        "decode_latency_ms": 0.0,
        "similarity_latency_ms": 0.0,
        "moderation_latency_ms": 0.0,
        "moderation_wait_latency_ms": 0.0,
    }
    cpu_capacity = max(2, os.cpu_count() or 2)
    decode_parallelism = min(32, cpu_capacity * (2 if context.accelerated else 1))
    semaphore = asyncio.Semaphore(max(1, decode_parallelism))

    async def _decode_frame(frame: ExtractedFrame) -> Image.Image | None:
        async with semaphore:
            try:
                return await _open_image_from_bytes(frame.data)
            except Exception as exc:
                print(f"[process_image_batch] Failed to open {frame.name}: {exc}")
                return None

    for frame in frame_payloads:
        decode_tasks.append(asyncio.create_task(_decode_frame(frame)))

    decode_started = time.perf_counter()
    decoded_images = await asyncio.gather(*decode_tasks)
    batch_metrics["decode_latency_ms"] += (
        time.perf_counter() - decode_started
    ) * 1000

    for frame, image in zip(frame_payloads, decoded_images):
        prepared.append((frame, image))
        if image is not None:
            valid_images.append(image)

    similarity_batches: List[List[dict[str, Any]]] = []
    if valid_images:
        try:
            similarity_started = time.perf_counter()
            similarity_batches = await asyncio.wait_for(
                asyncio.to_thread(clip_vectors.query_similar_batch, valid_images, 0),
                timeout=SIMILARITY_SEARCH_TIMEOUT_SECONDS,
            )
            batch_metrics["similarity_latency_ms"] += (
                time.perf_counter() - similarity_started
            ) * 1000
        except asyncio.TimeoutError:
            batch_metrics["similarity_latency_ms"] += (
                time.perf_counter() - similarity_started
            ) * 1000
            log.warning(
                "Batch similarity search exceeded %.1fs; skipping matches",
                SIMILARITY_SEARCH_TIMEOUT_SECONDS,
            )
            similarity_batches = [[] for _ in valid_images]
        except Exception as exc:
            log.warning(
                "Batch similarity search failed; continuing without matches: %s",
                exc,
                exc_info=True,
            )
            similarity_batches = [[] for _ in valid_images]

    results: List[Tuple[ExtractedFrame, dict[str, Any] | None]] = []
    similarity_iter = iter(similarity_batches)

    base_metadata = dict(payload_metadata or {})
    if context.guild_id is not None:
        base_metadata.setdefault("guild_id", context.guild_id)

    entries: list[
        tuple[
            ExtractedFrame,
            Image.Image | None,
            bytes | None,
            str | None,
            Optional[List[dict[str, Any]]],
            dict[str, Any] | None,
        ]
    ] = []

    for frame, image in prepared:
        similarity_response = next(similarity_iter, []) if image is not None else None
        payload_bytes: bytes | None = frame.data
        payload_mime: str | None = frame.mime_type
        frame_mime_lower = (frame.mime_type or "").lower()
        conversion_performed = (
            convert_to_png and image is not None and frame_mime_lower != "image/png"
        )
        if conversion_performed:
            payload_bytes = await asyncio.to_thread(_encode_image_to_png_bytes, image)
            payload_mime = "image/png"
        frame_metadata: dict[str, Any] | None = dict(base_metadata)
        frame_metadata.setdefault("input_kind", "image")
        frame_metadata["frame_name"] = frame.name
        frame_metadata["source_extension"] = (
            os.path.splitext(frame.name)[1].lower() if frame.name else None
        )
        frame_metadata["original_format"] = (
            getattr(image, "info", {}).get("original_format") if image is not None else None
        )
        frame_metadata["original_mime"] = frame.mime_type
        frame_metadata["conversion_performed"] = conversion_performed
        frame_metadata["payload_mime"] = payload_mime
        frame_metadata["passthrough"] = not conversion_performed
        frame_metadata["image_size"] = list(image.size) if image is not None else None
        frame_metadata["image_mode"] = getattr(image, "mode", None) if image is not None else None
        frame_metadata["source_bytes"] = (
            len(frame.data) if frame.data is not None else None
        )
        frame_metadata["payload_bytes"] = (
            len(payload_bytes)
            if payload_bytes is not None
            else (len(frame.data) if frame.data is not None else None)
        )
        frame_metadata["conversion_target"] = (
            "image/png" if conversion_performed else frame.mime_type
        )
        frame_metadata["conversion_reason"] = (
            "unsupported_format" if conversion_performed else None
        )
        frame_metadata["video_frame"] = True
        frame_index = getattr(frame, "index", None)
        if frame_index is not None:
            frame_metadata.setdefault("frame_index", frame_index)
        entries.append((frame, image, payload_bytes, payload_mime, similarity_response, frame_metadata))

    mod_api_limit = (
        ACCELERATED_MOD_API_MAX_CONCURRENCY
        if context.accelerated
        else MOD_API_MAX_CONCURRENCY
    )
    cpu_capacity = max(2, os.cpu_count() or 2)
    baseline = 8 if context.accelerated else 3
    dynamic_local_limit = max(baseline, min(cpu_capacity, mod_api_limit))
    max_local_concurrency = min(dynamic_local_limit, mod_api_limit)
    local_limit_candidates = [len(entries) or 1, max_local_concurrency, mod_api_limit]
    if isinstance(max_concurrent_frames, int) and max_concurrent_frames > 0:
        local_limit_candidates.append(max_concurrent_frames)
    local_moderation_semaphore = asyncio.Semaphore(max(1, min(local_limit_candidates)))

    async def _moderate_entry(
        frame: ExtractedFrame,
        image: Image.Image | None,
        payload_bytes: bytes | None,
        payload_mime: str | None,
        similarity_response: Optional[List[dict[str, Any]]],
        payload_metadata: dict[str, Any] | None,
    ) -> Tuple[ExtractedFrame, dict[str, Any] | None]:
        response: dict[str, Any] | None = None
        if image is not None:
            try:
                local_wait_started = time.perf_counter()
                async with local_moderation_semaphore:
                    local_acquired_at = time.perf_counter()
                    if local_acquired_at > local_wait_started:
                        batch_metrics["moderation_wait_latency_ms"] += (
                            local_acquired_at - local_wait_started
                        ) * 1000

                    def _record_rate_wait(elapsed_seconds: float) -> None:
                        batch_metrics["moderation_wait_latency_ms"] += (
                            max(elapsed_seconds, 0.0) * 1000
                        )

                    def _record_rate_duration(elapsed_seconds: float) -> None:
                        batch_metrics["moderation_latency_ms"] += (
                            max(elapsed_seconds, 0.0) * 1000
                        )

                    response = await _run_image_pipeline(
                        scanner,
                        image_path=None,
                        image=image,
                        context=context,
                        similarity_response=similarity_response,
                        image_bytes=payload_bytes,
                        image_mime=payload_mime,
                        payload_metadata=payload_metadata,
                        on_rate_limiter_acquire=_record_rate_wait,
                        on_rate_limiter_release=_record_rate_duration,
                    )
            finally:
                image.close()
        return frame, response

    if entries:
        results.extend(
            await asyncio.gather(
                *(
                    _moderate_entry(
                        frame,
                        image,
                        payload_bytes,
                        payload_mime,
                        similarity_response,
                        payload_metadata,
                    )
                    for frame, image, payload_bytes, payload_mime, similarity_response, payload_metadata in entries
                )
            )
        )

    return results, batch_metrics


__all__ = ["log", "process_image_batch"]
