from __future__ import annotations

import asyncio
import contextlib
import io
from typing import Optional

from modules.metrics.config import get_metrics_redis_config
from scripts.metrics_redis_tool import action_reset, redis

__all__ = ["reset_latency_averages"]


async def reset_latency_averages(*, pattern: Optional[str] = None, dry_run: bool = False) -> str:
    """Reset latency-related aggregates in the metrics Redis store.

    Returns a textual report of the updates performed.
    """
    config = get_metrics_redis_config()
    if not config.enabled or not config.url:
        raise RuntimeError("Metrics Redis is not configured.")

    def _run_reset() -> str:
        client = redis.Redis.from_url(config.url, decode_responses=True)
        try:
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                action_reset(client, config.key_prefix, pattern, dry_run=dry_run)
            return buffer.getvalue().strip()
        finally:
            client.close()

    return await asyncio.to_thread(_run_reset)
