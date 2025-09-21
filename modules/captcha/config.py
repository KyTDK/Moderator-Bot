from __future__ import annotations

from dataclasses import dataclass
import logging
import os
import socket
import uuid

_TRUE_VALUES = {"1", "true", "yes", "on"}
_logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CaptchaStreamConfig:
    """Configuration for the Redis stream listener that processes captcha callbacks."""

    enabled: bool
    redis_url: str | None
    stream: str
    group: str
    consumer_name: str
    block_ms: int
    batch_size: int
    max_requeue_attempts: int
    shared_secret: bytes | None

    @classmethod
    def from_env(cls) -> "CaptchaStreamConfig":
        redis_url = _resolve_redis_url()
        stream = os.getenv("CAPTCHA_CALLBACK_STREAM", "captcha:callbacks").strip() or "captcha:callbacks"
        group = os.getenv("CAPTCHA_CALLBACK_GROUP", "modbot-captcha")

        consumer_name = os.getenv("CAPTCHA_CALLBACK_CONSUMER")
        if not consumer_name:
            hostname = socket.gethostname() or "modbot"
            consumer_name = f"{hostname}:{os.getpid()}:{uuid.uuid4().hex[:6]}"

        block_ms = _coerce_positive_int(os.getenv("CAPTCHA_STREAM_BLOCK_MS")) or 10000
        batch_size = _coerce_positive_int(os.getenv("CAPTCHA_STREAM_BATCH_SIZE")) or 10
        max_requeue_attempts = _coerce_positive_int(os.getenv("CAPTCHA_STREAM_MAX_REQUEUE")) or 3

        shared_secret = _resolve_shared_secret()

        enabled_raw = os.getenv("CAPTCHA_STREAM_ENABLED")
        if enabled_raw is None:
            enabled = redis_url is not None
        else:
            enabled = enabled_raw.lower() in _TRUE_VALUES and redis_url is not None

        if enabled and redis_url is None:
            _logger.warning(
                "Captcha stream listener explicitly enabled but no Redis URL was provided; disabling listener.",
            )
            enabled = False

        if not enabled:
            _logger.info("Captcha Redis stream listener is disabled.")

        return cls(
            enabled=enabled,
            redis_url=redis_url,
            stream=stream,
            group=group,
            consumer_name=consumer_name,
            block_ms=block_ms,
            batch_size=batch_size,
            max_requeue_attempts=max_requeue_attempts,
            shared_secret=shared_secret,
        )


def _resolve_shared_secret() -> bytes | None:
    raw = os.getenv("CAPTCHA_SHARED_SECRET")
    return raw.encode("utf-8") if raw else None


def _resolve_redis_url() -> str | None:
    url = os.getenv("CAPTCHA_REDIS_URL") or os.getenv("REDIS_URL")
    if url:
        cleaned = url.strip()
        return cleaned or None

    host = os.getenv("CAPTCHA_REDIS_HOST")
    if not host:
        return None

    port = os.getenv("CAPTCHA_REDIS_PORT", "6379")
    db = os.getenv("CAPTCHA_REDIS_DB", "0")
    password = os.getenv("CAPTCHA_REDIS_PASSWORD")

    auth_part = ""
    if password:
        auth_part = f":{password}@"

    return f"redis://{auth_part}{host}:{port}/{db}"


def _coerce_positive_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None
