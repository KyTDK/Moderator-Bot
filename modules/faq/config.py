from __future__ import annotations

import os
import socket
import uuid
from dataclasses import dataclass

from modules.utils.redis_stream import RedisStreamConfig

_TRUE_VALUES = {"1", "true", "yes", "on"}


@dataclass(slots=True)
class FAQStreamConfig(RedisStreamConfig):
    """Configuration for the Redis stream that carries FAQ commands."""

    response_stream: str = ""
    max_response_length: int = 1000

    @classmethod
    def from_env(cls) -> "FAQStreamConfig":
        redis_url = _resolve_redis_url()
        command_stream = (
            os.getenv("FAQ_COMMAND_STREAM", "faq:commands").strip() or "faq:commands"
        )
        response_stream = (
            os.getenv("FAQ_RESPONSE_STREAM", "faq:responses").strip() or "faq:responses"
        )
        group = os.getenv("FAQ_STREAM_GROUP", "modbot-faq").strip() or "modbot-faq"

        consumer_name = os.getenv("FAQ_STREAM_CONSUMER")
        if not consumer_name:
            hostname = socket.gethostname() or "modbot"
            consumer_name = f"{hostname}:{os.getpid()}:{uuid.uuid4().hex[:6]}"

        block_ms = _coerce_positive_int(os.getenv("FAQ_STREAM_BLOCK_MS")) or 10000
        fetch_count = _coerce_positive_int(os.getenv("FAQ_STREAM_FETCH_COUNT")) or 20
        max_response_length = (
            _coerce_positive_int(os.getenv("FAQ_STREAM_RESPONSE_MAXLEN")) or 1000
        )

        enabled_raw = os.getenv("FAQ_STREAM_ENABLED")
        if enabled_raw is None:
            enabled = redis_url is not None
        else:
            enabled = enabled_raw.lower() in _TRUE_VALUES and redis_url is not None

        if enabled and redis_url is None:
            # Safety guard – refuse to enable without a target Redis instance.
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
        )


def _resolve_redis_url() -> str | None:
    raw = os.getenv("FAQ_REDIS_URL") or os.getenv("REDIS_URL")
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


__all__ = ["FAQStreamConfig"]
