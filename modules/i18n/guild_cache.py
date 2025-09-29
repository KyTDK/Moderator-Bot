from __future__ import annotations

"""Helpers for resolving guild locales from stored metadata."""

import logging
from typing import Any, Dict, Mapping, Optional

from .locale_utils import normalise_locale

logger = logging.getLogger(__name__)

def _coerce_int(value: Any) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None

def extract_guild_id(candidate: Any) -> int | None:
    guild_id: Any | None = None

    if isinstance(candidate, Mapping):
        guild = candidate.get("guild")
        if isinstance(guild, Mapping):
            guild_id = guild.get("id") or guild.get("guild_id")

        if guild_id is None and "guild_id" in candidate:
            guild_id = candidate.get("guild_id")

        if guild_id is None and "preferred_locale" in candidate and "id" in candidate:
            guild_id = candidate.get("id")
    else:
        guild = getattr(candidate, "guild", None)
        if isinstance(guild, Mapping):
            guild_id = guild.get("id") or guild.get("guild_id")
        elif guild is not None:
            guild_id = getattr(guild, "id", None)

        if guild_id is None:
            guild_id = getattr(candidate, "guild_id", None)

        if guild_id is None:
            candidate_id = getattr(candidate, "id", None)
            if candidate_id is not None and "guild" in type(candidate).__name__.lower():
                guild_id = candidate_id

    return _coerce_int(guild_id)

class GuildLocaleCache:
    def __init__(self) -> None:
        self._stored: Dict[int, Optional[str]] = {}
        self._overrides: Dict[int, Optional[str]] = {}

    def preload(self, values: Dict[int, Optional[str]]) -> None:
        for guild_id, locale in values.items():
            self.store(guild_id, locale)

    def store(self, guild_id: int, locale: Any) -> Optional[str]:
        normalized = normalise_locale(locale)
        self._stored[guild_id] = normalized
        logger.info(
            "Stored guild locale (guild_id=%s): input=%r normalized=%s",
            guild_id,
            locale,
            normalized,
        )
        return normalized

    def set_override(self, guild_id: int, locale: Any) -> Optional[str]:
        normalized = normalise_locale(locale)
        self._overrides[guild_id] = normalized
        logger.info(
            "Updated guild locale override (guild_id=%s): %r -> %s",
            guild_id,
            locale,
            normalized,
        )
        return normalized

    def get(self, guild_id: int) -> Optional[str]:
        return self._stored.get(guild_id)

    def get_override(self, guild_id: int) -> Optional[str]:
        return self._overrides.get(guild_id)

    def drop(self, guild_id: int) -> None:
        self._stored.pop(guild_id, None)
        self._overrides.pop(guild_id, None)

__all__ = ["GuildLocaleCache", "extract_guild_id"]
