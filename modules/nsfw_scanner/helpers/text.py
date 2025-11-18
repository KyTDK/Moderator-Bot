from __future__ import annotations

import random
from typing import Any, Optional

from modules.utils import text_vectors

from ..constants import (
    ADD_SFW_VECTOR,
    TEXT_SIMILARITY_THRESHOLD,
    VECTOR_REFRESH_DIVISOR,
)
from ..utils.categories import is_allowed_category
from .context import ImageProcessingContext, build_image_processing_context
from .moderation import moderator_api
from .vector_tasks import (
    schedule_text_vector_add,
    schedule_text_vector_delete,
)


async def process_text(
    scanner,
    text: str,
    guild_id: int | None = None,
    *,
    settings: dict[str, Any] | None = None,
    context: ImageProcessingContext | None = None,
    similarity_response: Optional[list[dict[str, Any]]] = None,
    payload_metadata: dict[str, Any] | None = None,
    queue_name: str | None = None,
) -> dict[str, Any] | None:
    content = (text or "").strip()
    if not content:
        return None

    ctx = context
    if ctx is None:
        ctx = await build_image_processing_context(
            guild_id,
            settings=settings,
            queue_name=queue_name,
        )

    metadata = dict(payload_metadata or {})
    metadata.setdefault("input_kind", "text")
    metadata.setdefault("text_chars", len(content))

    matches = similarity_response
    if matches is None:
        matches = text_vectors.query_similar(content, threshold=0)

    best_match = None
    max_similarity = 0.0
    max_category = None
    if matches:
        best_match = max(matches, key=lambda item: float(item.get("similarity", 0) or 0))
        max_similarity = float(best_match.get("similarity", 0) or 0)
        max_category = best_match.get("category")

    refresh_triggered = (
        best_match
        and VECTOR_REFRESH_DIVISOR > 0
        and random.randint(1, VECTOR_REFRESH_DIVISOR) == 1
    )
    if refresh_triggered:
        vector_id = best_match.get("vector_id")
        if vector_id is not None:
            schedule_text_vector_delete(vector_id)

    milvus_available = text_vectors.is_available()
    allow_similarity_shortcut = matches and not refresh_triggered

    if allow_similarity_shortcut:
        for match in matches:
            similarity = float(match.get("similarity", 0) or 0)
            if similarity < TEXT_SIMILARITY_THRESHOLD:
                continue

            category = match.get("category")
            if not category:
                return {
                    "is_nsfw": False,
                    "reason": "similarity_match",
                    "max_similarity": max_similarity,
                    "max_category": max_category,
                    "text_threshold": TEXT_SIMILARITY_THRESHOLD,
                    "similarity": similarity,
                }

            if is_allowed_category(category, ctx.text_allowed_categories):
                return {
                    "is_nsfw": True,
                    "category": category,
                    "reason": "similarity_match",
                    "max_similarity": max_similarity,
                    "max_category": max_category,
                    "text_threshold": TEXT_SIMILARITY_THRESHOLD,
                    "similarity": similarity,
                }

    skip_vector_add = (
        max_similarity >= TEXT_SIMILARITY_THRESHOLD and not refresh_triggered
    ) or not milvus_available

    response = await moderator_api(
        scanner,
        text=content,
        guild_id=ctx.guild_id,
        skip_vector_add=skip_vector_add,
        max_similarity=max_similarity,
        allowed_categories=ctx.text_allowed_categories,
        threshold=ctx.text_moderation_threshold,
        payload_metadata=metadata,
        accelerated=ctx.accelerated,
        queue_name=queue_name or ctx.queue_name,
    )

    if isinstance(response, dict):
        response.setdefault("max_similarity", max_similarity)
        response.setdefault("max_category", max_category)
        response.setdefault("text_threshold", TEXT_SIMILARITY_THRESHOLD)
        return response

    return response
