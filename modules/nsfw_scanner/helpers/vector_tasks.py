from __future__ import annotations

import asyncio
import logging
from typing import Any

from PIL import Image

from modules.utils import clip_vectors

from .background_tasks import schedule_background_task


log = logging.getLogger(__name__)


def schedule_vector_delete(
    vector_id: int | None,
    *,
    logger: logging.Logger | None = None,
) -> None:
    """Schedule a best-effort deletion of *vector_id* from the vector store."""
    if vector_id is None:
        return

    active_logger = logger or log

    async def _run() -> None:
        await clip_vectors.delete_vectors([vector_id])

    schedule_background_task(
        _run(),
        logger=active_logger,
        purpose=f"vector delete ({vector_id})",
    )


def schedule_vector_add(
    image: Image.Image,
    metadata: dict[str, Any],
    *,
    logger: logging.Logger | None = None,
) -> None:
    """Copy *image* and enqueue a vector store insertion with *metadata*."""
    active_logger = logger or log
    try:
        image_copy = image.copy()
        image_copy.load()
    except Exception as exc:
        active_logger.debug(
            "Skipping vector insert; unable to copy image: %s",
            exc,
            exc_info=True,
        )
        return

    async def _run() -> None:
        try:
            await asyncio.to_thread(clip_vectors.add_vector, image_copy, metadata)
        finally:
            try:
                image_copy.close()
            except Exception:
                pass

    schedule_background_task(
        _run(),
        logger=active_logger,
        purpose="vector add",
    )
