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
    start_id: str
    shared_secret: bytes | None
    pending_auto_claim_ms: int
    max_concurrency: int = 5

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

        pending_idle_raw = os.getenv("CAPTCHA_STREAM_PENDING_IDLE_MS")
        pending_auto_claim_ms = 5000
        if pending_idle_raw is not None:
            try:
                parsed_pending = int(str(pending_idle_raw).strip())
            except (TypeError, ValueError):
                parsed_pending = None
            if parsed_pending is not None:
                pending_auto_claim_ms = max(parsed_pending, 0)

        max_concurrency = _coerce_positive_int(os.getenv("CAPTCHA_STREAM_MAX_CONCURRENCY")) or 5
        max_concurrency = max(1, max_concurrency)

        shared_secret = _resolve_shared_secret()
        start_id = _resolve_stream_start_id(os.getenv("CAPTCHA_STREAM_START_ID"))

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
            start_id=start_id,
            shared_secret=shared_secret,
            pending_auto_claim_ms=pending_auto_claim_ms,
            max_concurrency=max_concurrency,
        )


def _resolve_shared_secret() -> bytes | None:
    raw = os.getenv("CAPTCHA_SHARED_SECRET")
    return raw.encode("utf-8") if raw else None


def _resolve_stream_start_id(raw: str | None) -> str:
    if raw is None:
        return "$"

    text = str(raw).strip()
    if not text:
        return "$"

    lowered = text.lower()
    if lowered in {"$", "latest", "new"}:
        return "$"
    if lowered in {"0", "zero", "start", "history", "from_start"}:
        return "0"
    return text


def _resolve_redis_url() -> str | None:
    url = os.getenv("CAPTCHA_REDIS_URL") or os.getenv("REDIS_URL")
    if not url:
        return None

    cleaned = url.strip()
    return cleaned or None


def _coerce_positive_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None
