from __future__ import annotations

from typing import Any

__all__ = ["AttachmentSettingsCache", "format_queue_wait_label"]

_CACHE_MISS = object()


def format_queue_wait_label(queue_name: str | None) -> str | None:
    """Convert a raw queue identifier into a human readable label."""
    if not queue_name:
        return None
    pretty = queue_name.replace("_", " ").strip()
    if not pretty:
        return None
    if "queue" not in pretty.lower():
        pretty = f"{pretty} queue"
    return f"{pretty.title()} wait"


class AttachmentSettingsCache:
    """Cache frequently accessed guild settings for a scan batch."""

    __slots__ = (
        "scan_settings",
        "nsfw_verbose",
        "check_tenor_gifs",
        "premium_status",
        "premium_plan",
        "text_enabled",
        "accelerated",
    )

    def __init__(self) -> None:
        self.scan_settings: Any = _CACHE_MISS
        self.nsfw_verbose: Any = _CACHE_MISS
        self.check_tenor_gifs: Any = _CACHE_MISS
        self.premium_status: Any = _CACHE_MISS
        self.premium_plan: Any = _CACHE_MISS
        self.text_enabled: Any = _CACHE_MISS
        self.accelerated: Any = _CACHE_MISS

    def has_scan_settings(self) -> bool:
        return self.scan_settings is not _CACHE_MISS

    def get_scan_settings(self) -> dict[str, Any] | None:
        if self.scan_settings is _CACHE_MISS:
            return None
        return self.scan_settings or {}

    def set_scan_settings(self, value: dict[str, Any] | None) -> None:
        self.scan_settings = value or {}

    def has_verbose(self) -> bool:
        return self.nsfw_verbose is not _CACHE_MISS

    def get_verbose(self) -> bool | None:
        if self.nsfw_verbose is _CACHE_MISS:
            return None
        return bool(self.nsfw_verbose)

    def set_verbose(self, value: bool | None) -> None:
        self.nsfw_verbose = bool(value)

    def has_check_tenor(self) -> bool:
        return self.check_tenor_gifs is not _CACHE_MISS

    def get_check_tenor(self) -> bool | None:
        if self.check_tenor_gifs is _CACHE_MISS:
            return None
        return bool(self.check_tenor_gifs)

    def set_check_tenor(self, value: bool | None) -> None:
        self.check_tenor_gifs = bool(value)

    def has_premium_status(self) -> bool:
        return self.premium_status is not _CACHE_MISS

    def get_premium_status(self) -> Any:
        if self.premium_status is _CACHE_MISS:
            return None
        return self.premium_status

    def set_premium_status(self, value: Any) -> None:
        self.premium_status = value if value is not None else {}

    def has_premium_plan(self) -> bool:
        return self.premium_plan is not _CACHE_MISS

    def get_premium_plan(self) -> Any:
        if self.premium_plan is _CACHE_MISS:
            return None
        return self.premium_plan

    def set_premium_plan(self, value: Any) -> None:
        self.premium_plan = value

    def has_text_enabled(self) -> bool:
        return self.text_enabled is not _CACHE_MISS

    def get_text_enabled(self) -> bool | None:
        if self.text_enabled is _CACHE_MISS:
            return None
        return bool(self.text_enabled)

    def set_text_enabled(self, value: Any) -> None:
        self.text_enabled = bool(value)

    def has_accelerated(self) -> bool:
        return self.accelerated is not _CACHE_MISS

    def get_accelerated(self) -> bool | None:
        if self.accelerated is _CACHE_MISS:
            return None
        return bool(self.accelerated)

    def set_accelerated(self, value: Any) -> None:
        self.accelerated = bool(value)
