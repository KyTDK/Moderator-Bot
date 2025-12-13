import asyncio
import logging
import random
import time
from typing import Any, Callable, List, Optional

from modules.nsfw_scanner.constants import (
    CLIP_THRESHOLD,
    HIGH_ACCURACY_SIMILARITY,
    SIMILARITY_SEARCH_TIMEOUT_SECONDS,
    VECTOR_REFRESH_DIVISOR,
)
from modules.utils import clip_vectors
from PIL import Image

from ..utils.categories import is_allowed_category
from .metrics import LatencyTracker
from .moderation import moderator_api
from .context import ImageProcessingContext
from .vector_tasks import schedule_vector_delete

log = logging.getLogger(__name__)


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def _run_image_pipeline(
    scanner,
    *,
    image_path: str | None,
    image: Image.Image,
    context: ImageProcessingContext,
    similarity_response: Optional[List[dict[str, Any]]] = None,
    image_bytes: bytes | None = None,
    image_mime: str | None = None,
    payload_metadata: dict[str, Any] | None = None,
    on_rate_limiter_acquire: Callable[[float], None] | None = None,
    on_rate_limiter_release: Callable[[float], None] | None = None,
) -> dict[str, Any] | None:
    latency_tracker = LatencyTracker()

    def _add_step(name: str, duration: float | None, *, label: str | None = None) -> None:
        if duration is None:
            return
        latency_tracker.record_step(name, duration, label=label)

    def _finalize(payload: dict[str, Any]) -> dict[str, Any]:
        pipeline_metrics = payload.setdefault("pipeline_metrics", {})
        pipeline_metrics, _ = latency_tracker.merge_into_pipeline(pipeline_metrics)
        payload["pipeline_metrics"] = pipeline_metrics
        return payload

    milvus_available = clip_vectors.is_available()
    vector_search_online = milvus_available and not clip_vectors.is_fallback_active()

    similarity_results = similarity_response
    if similarity_results is None:
        if vector_search_online:
            similarity_started = time.perf_counter()
            try:
                similarity_results = await asyncio.wait_for(
                    asyncio.to_thread(clip_vectors.query_similar, image, threshold=0),
                    timeout=SIMILARITY_SEARCH_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                log.warning(
                    "Similarity search exceeded %.1fs; falling back to moderator API",
                    SIMILARITY_SEARCH_TIMEOUT_SECONDS,
                )
                similarity_results = []
            except Exception as exc:
                log.warning(
                    "Similarity search failed; falling back to moderator API: %s",
                    exc,
                    exc_info=True,
                )
                similarity_results = []
            finally:
                _add_step(
                    "similarity_search",
                    (time.perf_counter() - similarity_started) * 1000,
                    label="Similarity Search",
                )
        else:
            similarity_results = []
            _add_step("similarity_search", 0.0, label="Similarity Search (offline)")

    best_match = None
    max_similarity = 0.0
    max_category = None
    if similarity_results:
        best_match = max(
            similarity_results,
            key=lambda item: float(item.get("similarity", 0) or 0),
        )
        max_similarity = float(best_match.get("similarity", 0) or 0)
        max_category = best_match.get("category")

    refresh_triggered = (
        best_match
        and not context.accelerated
        and VECTOR_REFRESH_DIVISOR > 0
        and random.randint(1, VECTOR_REFRESH_DIVISOR) == 1
    )
    if refresh_triggered:
        vector_id = best_match.get("vector_id")
        if vector_id is not None:
            latency_tracker.merge_steps(
                {
                    "vector_delete_async": {
                        "duration_ms": 0.0,
                        "label": "Vector Delete (async)",
                    }
                }
            )
            schedule_vector_delete(vector_id, logger=log)

    allow_similarity_shortcut = (
        similarity_results
        and not refresh_triggered
        and (
            not context.high_accuracy
            or max_similarity >= HIGH_ACCURACY_SIMILARITY
        )
    )

    if allow_similarity_shortcut:
        for item in similarity_results:
            similarity = float(item.get("similarity", 0) or 0)
            if similarity < CLIP_THRESHOLD:
                continue

            if item.get("custom_block"):
                vector_guild_id = _coerce_int(item.get("guild_id"))
                if context.guild_id is None or vector_guild_id != context.guild_id:
                    continue
                effective_category = item.get("category") or "custom_block"
                effective_max_similarity = max(max_similarity, similarity)
                result = {
                    "is_nsfw": True,
                    "category": effective_category,
                    "reason": "custom_block_match",
                    "max_similarity": effective_max_similarity,
                    "max_category": effective_category,
                    "high_accuracy": context.high_accuracy,
                    "clip_threshold": CLIP_THRESHOLD,
                    "similarity": similarity,
                    "custom_block": True,
                }
                if item.get("label"):
                    result["custom_block_label"] = item.get("label")
                vector_id = item.get("vector_id")
                if vector_id is not None:
                    result["vector_id"] = vector_id
                return _finalize(result)

            category = item.get("category")
            if not category:
                result = {
                    "is_nsfw": False,
                    "reason": "similarity_match",
                    "max_similarity": max_similarity,
                    "max_category": max_category,
                    "high_accuracy": context.high_accuracy,
                    "clip_threshold": CLIP_THRESHOLD,
                    "similarity": similarity,
                }
                return _finalize(result)

            if is_allowed_category(category, context.allowed_categories):
                result = {
                    "is_nsfw": True,
                    "category": category,
                    "reason": "similarity_match",
                    "max_similarity": max_similarity,
                    "max_category": max_category,
                    "high_accuracy": context.high_accuracy,
                    "clip_threshold": CLIP_THRESHOLD,
                    "similarity": similarity,
                }
                return _finalize(result)

    skip_vector = (
        max_similarity >= CLIP_THRESHOLD and not refresh_triggered
    ) or not vector_search_online
    moderation_started = time.perf_counter()
    response = await moderator_api(
        scanner,
        image_path=image_path,
        image_bytes=image_bytes,
        image_mime=image_mime,
        guild_id=context.guild_id,
        image=image,
        skip_vector_add=skip_vector,
        max_similarity=max_similarity,
        allowed_categories=context.allowed_categories,
        threshold=context.moderation_threshold,
        payload_metadata=payload_metadata,
        on_rate_limiter_acquire=on_rate_limiter_acquire,
        on_rate_limiter_release=on_rate_limiter_release,
        accelerated=context.accelerated,
        queue_name=context.queue_name,
    )
    _add_step(
        "moderation_api",
        (time.perf_counter() - moderation_started) * 1000,
        label="Moderator API",
    )
    if isinstance(response, dict):
        pipeline_metrics = response.setdefault("pipeline_metrics", {})
        if isinstance(pipeline_metrics, dict):
            breakdown = pipeline_metrics.get("moderator_breakdown_ms")
            if breakdown:
                latency_tracker.merge_steps(breakdown)
        response.setdefault("max_similarity", max_similarity)
        response.setdefault("max_category", max_category)
        response.setdefault("high_accuracy", context.high_accuracy)
        response.setdefault("clip_threshold", CLIP_THRESHOLD)
        return _finalize(response)
    return response


__all__ = ["log", "_run_image_pipeline"]
