from __future__ import annotations

import aiohttp
from discord.errors import NotFound


def truncate(value: str, limit: int = 1024) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "\u2026"


def should_suppress_download_failure(exc: Exception) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    if isinstance(exc, aiohttp.ClientResponseError):
        status = getattr(exc, "status", None)
        return status in {404, 410, 451}
    if isinstance(exc, (FileNotFoundError, NotFound)):
        return True
    return False


def normalize_source_url(url: str | None) -> str | None:
    """Strip Discord-specific markup that can wrap media URLs."""
    if not url:
        return url
    cleaned = url.strip()
    if not cleaned:
        return None

    while True:
        updated = cleaned
        if updated.startswith("||"):
            updated = updated[2:].lstrip()
        if updated.endswith("||"):
            updated = updated[:-2].rstrip()
        if updated.startswith("<") and updated.endswith(">") and len(updated) >= 2:
            updated = updated[1:-1].strip()
        if updated == cleaned:
            break
        cleaned = updated
        if not cleaned:
            return None
    return cleaned


__all__ = ["normalize_source_url", "should_suppress_download_failure", "truncate"]
