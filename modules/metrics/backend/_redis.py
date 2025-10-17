from __future__ import annotations

import asyncio
import logging
from typing import Optional

from ..config import get_metrics_redis_config

try:  # pragma: no cover - module availability depends on runtime environment
    from redis.asyncio import Redis as RedisClient
    from redis.asyncio import from_url as redis_from_url
    from redis.exceptions import RedisError
except Exception:  # pragma: no cover - fallback when redis-py is unavailable
    RedisClient = None  # type: ignore[assignment]
    redis_from_url = None  # type: ignore[assignment]

    class RedisError(Exception):  # type: ignore[assignment]
        """Fallback Redis error when redis-py is unavailable."""

        pass

_logger = logging.getLogger(__name__)

_redis_client: Optional["RedisClient"] = None
_client_override: Optional["RedisClient"] = None
_client_lock = asyncio.Lock()


def set_client_override(client: "RedisClient | None") -> None:
    """Install an in-memory Redis substitute (primarily for tests)."""
    global _client_override, _redis_client
    _client_override = client
    if client is not None:
        _redis_client = None


async def close_metrics_client() -> None:
    """Close the cached Redis client if one was created."""
    global _redis_client
    if _redis_client is None:
        return
    client = _redis_client
    _redis_client = None
    try:
        await client.close()
        await client.connection_pool.disconnect()
    except Exception:  # pragma: no cover - best effort cleanup
        pass


async def get_redis_client() -> "RedisClient":
    """Return the Redis client configured for metrics storage."""
    if _client_override is not None:
        return _client_override

    config = get_metrics_redis_config()
    if not config.enabled:
        raise RuntimeError("Metrics Redis configuration is not enabled (METRICS_REDIS_URL missing)")
    if RedisClient is None or redis_from_url is None:
        raise RuntimeError("redis-py is not installed; metrics backend cannot operate")

    global _redis_client
    if _redis_client is not None:
        return _redis_client

    async with _client_lock:
        if _redis_client is None:
            _redis_client = redis_from_url(config.url, decode_responses=True)
        return _redis_client


__all__ = [
    "RedisClient",
    "RedisError",
    "close_metrics_client",
    "get_redis_client",
    "set_client_override",
]
