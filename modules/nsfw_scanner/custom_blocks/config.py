from __future__ import annotations

import os
import socket
import uuid
from dataclasses import dataclass

from modules.utils.redis_stream import RedisStreamConfig

_TRUE_VALUES = {"1", "true", "yes", "on"}


@dataclass(slots=True)
class CustomBlockStreamConfig(RedisStreamConfig):
    """Configuration for the Redis stream that carries dashboard custom image blocks."""

    response_stream: str = ""
    max_response_length: int = 1000
    max_image_bytes: int = 8 * 1024 * 1024
    download_timeout: float = 15.0

    @classmethod
    def from_env(cls) -> "CustomBlockStreamConfig":
        redis_url = _resolve_redis_url()
        command_stream = (
            os.getenv("CUSTOM_BLOCK_COMMAND_STREAM", "nsfw:custom-blocks:commands").strip()
            or "nsfw:custom-blocks:commands"
        )
        response_stream = (
            os.getenv("CUSTOM_BLOCK_RESPONSE_STREAM", "nsfw:custom-blocks:responses").strip()
            or "nsfw:custom-blocks:responses"
        )
        group = (
            os.getenv("CUSTOM_BLOCK_STREAM_GROUP", "modbot-custom-blocks").strip()
            or "modbot-custom-blocks"
        )

        consumer_name = os.getenv("CUSTOM_BLOCK_STREAM_CONSUMER")
        if not consumer_name:
            hostname = socket.gethostname() or "modbot"
            consumer_name = f"{hostname}:{os.getpid()}:{uuid.uuid4().hex[:6]}"

        block_ms = _coerce_positive_int(os.getenv("CUSTOM_BLOCK_STREAM_BLOCK_MS")) or 10_000
        fetch_count = _coerce_positive_int(os.getenv("CUSTOM_BLOCK_STREAM_FETCH_COUNT")) or 5
        max_response_length = (
            _coerce_positive_int(os.getenv("CUSTOM_BLOCK_STREAM_RESPONSE_MAXLEN")) or 1_000
        )
        max_image_bytes = (
            _coerce_positive_int(os.getenv("CUSTOM_BLOCK_MAX_IMAGE_BYTES")) or (8 * 1024 * 1024)
        )
        download_timeout = (
            _coerce_positive_float(os.getenv("CUSTOM_BLOCK_DOWNLOAD_TIMEOUT_SECONDS")) or 15.0
        )

        enabled_raw = os.getenv("CUSTOM_BLOCK_STREAM_ENABLED")
        if enabled_raw is None:
            enabled = redis_url is not None
        else:
            enabled = enabled_raw.strip().lower() in _TRUE_VALUES and redis_url is not None

        if enabled and redis_url is None:
            enabled = False

        return cls(
            enabled=enabled,
            redis_url=redis_url,
            stream=command_stream,
            group=group,
            consumer_name=consumer_name,
            block_ms=block_ms,
            fetch_count=fetch_count,
            max_concurrency=1,
            response_stream=response_stream,
            max_response_length=max_response_length,
            max_image_bytes=max(1, int(max_image_bytes)),
            download_timeout=max(1.0, float(download_timeout)),
        )


def _resolve_redis_url() -> str | None:
    raw = os.getenv("CUSTOM_BLOCK_REDIS_URL") or os.getenv("REDIS_URL")
    if not raw:
        return None
    cleaned = raw.strip()
    return cleaned or None


def _coerce_positive_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _coerce_positive_float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


__all__ = ["CustomBlockStreamConfig"]
