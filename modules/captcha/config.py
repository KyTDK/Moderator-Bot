from __future__ import annotations

from dataclasses import dataclass
import logging
import os

_TRUE_VALUES = {"1", "true", "yes", "on"}
_logger = logging.getLogger(__name__)

@dataclass(slots=True)
class CaptchaWebhookConfig:
    """Configuration for the local captcha webhook endpoint."""

    enabled: bool
    host: str
    port: int
    token: str | None
    shared_secret: bytes | None

    @classmethod
    def from_env(cls) -> "CaptchaWebhookConfig":
        host = os.getenv("CAPTCHA_WEBHOOK_HOST", "0.0.0.0")
        port_raw = os.getenv("CAPTCHA_WEBHOOK_PORT", "8080")
        try:
            port = int(port_raw)
            if port <= 0 or port > 65535:
                raise ValueError
        except (TypeError, ValueError):
            _logger.warning("Invalid CAPTCHA_WEBHOOK_PORT=%s; falling back to 8080", port_raw)
            port = 8080

        token = os.getenv("CAPTCHA_WEBHOOK_TOKEN") or os.getenv("CAPTCHA_API_TOKEN")
        shared_secret_raw = os.getenv("CAPTCHA_SHARED_SECRET")
        shared_secret = shared_secret_raw.encode("utf-8") if shared_secret_raw else None

        enabled_raw = os.getenv("CAPTCHA_WEBHOOK_ENABLED")
        if enabled_raw is None:
            enabled = token is not None or shared_secret is not None
        else:
            enabled = enabled_raw.lower() in _TRUE_VALUES

        if not enabled:
            return cls(
                enabled=False,
                host=host,
                port=port,
                token=token,
                shared_secret=shared_secret,
            )

        if token is None:
            _logger.warning(
                "Captcha webhook enabled without CAPTCHA_WEBHOOK_TOKEN; requests will not be authenticated."
            )

        if shared_secret is None:
            _logger.warning(
                "Captcha webhook enabled without CAPTCHA_SHARED_SECRET; callback signatures cannot be verified."
            )

        return cls(
            enabled=True,
            host=host,
            port=port,
            token=token,
            shared_secret=shared_secret,
        )
