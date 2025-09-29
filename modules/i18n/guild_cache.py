from __future__ import annotations

"""Helpers for resolving guild locales from stored metadata."""

import logging
from typing import Any, Dict, Iterable, Optional

from .locale_utils import normalise_locale

logger = logging.getLogger(__name__)


def extract_guild_id(candidate: Any) -> int | None:
    guild = getattr(candidate, "guild", None)
    guild_id = getattr(guild, "id", None) if guild is not None else None
    if guild_id is None:
        guild_id = getattr(candidate, "guild_id", None)

    if guild_id is None:
        return None

    try:
        return int(guild_id)
    except (TypeError, ValueError):
        return None


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

    def resolve(self, candidate: Any) -> Optional[str]:
        guild_id = extract_guild_id(candidate)
        if guild_id is None:
            logger.info("Could not resolve guild id from candidate %r", candidate)
            return None

        override = self._overrides.get(guild_id)
        if override:
            logger.info("Resolved locale via override (guild_id=%s): %s", guild_id, override)
            return override

        stored = self._stored.get(guild_id)
        if stored:
            logger.info("Resolved locale via stored cache (guild_id=%s): %s", guild_id, stored)
            return stored

        logger.info("No locale found for guild_id=%s", guild_id)
        return None

    def resolve_from_candidates(self, candidates: Iterable[Any]) -> Optional[str]:
        for candidate in candidates:
            locale = self.resolve(candidate)
            if locale:
                return locale
        logger.info("Failed to resolve locale from provided candidates")
        return None

    def drop(self, guild_id: int) -> None:
        self._stored.pop(guild_id, None)
        self._overrides.pop(guild_id, None)


__all__ = ["GuildLocaleCache", "extract_guild_id"]

