from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable


def schedule_background_task(
    coro: Awaitable[Any],
    *,
    logger: logging.Logger,
    purpose: str,
) -> None:
    """Execute *coro* in the background while surfacing failures via *logger*."""
    try:
        task = asyncio.create_task(coro)
    except RuntimeError:
        logger.debug("Unable to schedule %s; no running event loop", purpose)
        return

    def _handle(completed: asyncio.Task[Any]) -> None:
        try:
            completed.result()
        except Exception:
            logger.debug("%s failed in background", purpose, exc_info=True)

    task.add_done_callback(_handle)
