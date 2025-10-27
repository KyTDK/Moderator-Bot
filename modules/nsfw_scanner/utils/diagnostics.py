from __future__ import annotations

import time
from typing import Iterable, Mapping

import discord

__all__ = [
    "DiagnosticRateLimiter",
    "extract_context_lines",
    "render_detail_lines",
    "truncate_field_value",
]


class DiagnosticRateLimiter:
    """Utility for throttling diagnostics by key."""

    def __init__(self, *, cooldown_seconds: float = 120.0) -> None:
        self.cooldown_seconds = cooldown_seconds
        self._last_emitted: dict[str, float] = {}

    def should_emit(self, key: str) -> bool:
        now = time.monotonic()
        last = self._last_emitted.get(key)
        if last is not None and (now - last) < self.cooldown_seconds:
            return False
        self._last_emitted[key] = now
        return True


def truncate_field_value(value: object, *, limit: int = 1024) -> str:
    """Truncate an embed field value to Discord's limit, appending an ellipsis."""

    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    if len(value) > limit:
        return f"{value[: limit - 1]}…"
    return value


def render_detail_lines(
    details: Mapping[str, object] | Iterable[tuple[str, object]] | None,
) -> str | None:
    if not details:
        return None

    items: Iterable[tuple[str, object]]
    if isinstance(details, Mapping):
        items = details.items()
    else:
        items = details

    lines = [f"{key}: {value}" for key, value in items if value is not None]
    if not lines:
        return None
    return truncate_field_value("\n".join(lines))


def extract_context_lines(
    *,
    metadata: Mapping[str, object] | None = None,
    fallback_guild_id: int | None = None,
    message: discord.Message | None = None,
    include_attachment: bool = True,
    include_author: bool = False,
    include_message: bool = True,
) -> list[str]:
    lines: list[str] = []
    seen: set[tuple[str, object]] = set()

    def _add(label: str, value: object | None) -> None:
        if value is None:
            return
        key = (label, value)
        if key in seen:
            return
        seen.add(key)
        lines.append(f"{label}: {value}")

    if metadata:
        _add("Guild", metadata.get("guild_id") or fallback_guild_id)
        _add("Channel", metadata.get("channel_id"))
        if include_message:
            _add("Message", metadata.get("message_id"))
        if include_attachment:
            _add("Attachment", metadata.get("attachment_id"))
    elif fallback_guild_id is not None:
        _add("Guild", fallback_guild_id)

    if message is not None:
        guild_id = getattr(getattr(message, "guild", None), "id", None)
        channel_id = getattr(getattr(message, "channel", None), "id", None)
        message_id = getattr(message, "id", None)
        author_id = getattr(getattr(message, "author", None), "id", None)

        _add("Guild", guild_id)
        _add("Channel", channel_id)
        if include_message:
            _add("Message", message_id)
        if include_author:
            _add("Author", author_id)

    return lines

