from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from urllib.parse import urlsplit, urlunsplit

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
    public_url: str | None

    @property
    def callback_url(self) -> str | None:
        if self.public_url is None:
            return None
        return f"{self.public_url.rstrip('/')}/captcha/callback"

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
        public_url = _resolve_public_url(host, port)

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
                public_url=public_url,
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
            public_url=public_url,
        )


def _resolve_public_url(host: str, port: int) -> str | None:
    raw = os.getenv("CAPTCHA_WEBHOOK_PUBLIC_URL")
    if raw:
        normalized = _normalize_public_url(raw)
        if normalized:
            return normalized
        _logger.warning("Invalid CAPTCHA_WEBHOOK_PUBLIC_URL=%s; ignoring", raw)

    host_normalized = host.strip().lower()
    loopback_hosts = {"127.0.0.1", "localhost"}
    if host_normalized in loopback_hosts:
        if host_normalized == "localhost":
            host_value = "localhost"
        else:
            host_value = "127.0.0.1"
        return f"http://{host_value}:{port}"

    return None


def _normalize_public_url(value: str) -> str | None:
    stripped = value.strip()
    if not stripped:
        return None

    candidate = stripped
    if "://" not in candidate:
        candidate = f"http://{candidate}"

    parsed = urlsplit(candidate)
    if not parsed.netloc:
        return None

    path = parsed.path.rstrip("/")
    rebuilt = parsed._replace(path=path, query="", fragment="")
    return urlunsplit(rebuilt)