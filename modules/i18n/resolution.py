"""Helpers for inferring locales from Discord events and cached overrides."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .guild_cache import GuildLocaleCache, extract_guild_id
from .locale_utils import normalise_locale


@dataclass(slots=True)
class LocaleResolution:
    """Represents the sources considered when resolving a locale."""

    override: str | None = None
    stored: str | None = None
    detected: str | None = None

    def resolved(self) -> str | None:
        """Return the preferred locale using override → stored → detected priority."""

        if self.override:
            return self.override
        if self.stored:
            return self.stored
        if self.detected:
            return self.detected
        return None

    def source(self) -> str | None:
        """Return the label describing where :meth:`resolved` originated."""

        if self.override:
            return "override"
        if self.stored:
            return "stored"
        if self.detected:
            return "detected"
        return None


def _detect_from_mapping(mapping: Mapping[str, Any]) -> str | None:
    for key in ("locale", "guild_locale", "user_locale", "preferred_locale"):
        if key in mapping:
            candidate = normalise_locale(mapping[key])
            if candidate:
                return candidate

    guild = mapping.get("guild")
    if guild is not None:
        nested = detect_locale(guild)
        if nested:
            return nested

    return None


def detect_locale(candidate: Any) -> str | None:
    """Extract a locale hint from *candidate* if one is present."""

    if candidate is None:
        return None

    if isinstance(candidate, Mapping):
        return _detect_from_mapping(candidate)

    for attribute in ("locale", "guild_locale", "user_locale", "preferred_locale"):
        value = getattr(candidate, attribute, None)
        detected = normalise_locale(value)
        if detected:
            return detected

    guild = getattr(candidate, "guild", None)
    if guild is not None and guild is not candidate:
        nested = detect_locale(guild)
        if nested:
            return nested

    return None


class LocaleResolver:
    """Resolve locales using configured overrides and automatic detection."""

    def __init__(self, cache: GuildLocaleCache) -> None:
        self._cache = cache

    def infer(self, *candidates: Any) -> LocaleResolution:
        return self.infer_from_iterable(candidates)

    def infer_from_iterable(self, candidates: Iterable[Any]) -> LocaleResolution:
        resolution = LocaleResolution()

        for candidate in candidates:
            if candidate is None:
                continue

            guild_id = extract_guild_id(candidate)
            if guild_id is not None:
                if resolution.override is None:
                    resolution.override = self._cache.get_override(guild_id)
                if resolution.stored is None:
                    resolution.stored = self._cache.get(guild_id)

                if resolution.override:
                    detected = detect_locale(candidate)
                    if resolution.detected is None and detected:
                        resolution.detected = detected
                    break

            if resolution.detected is None:
                detected = detect_locale(candidate)
                if detected:
                    resolution.detected = detected

        return resolution


__all__ = [
    "LocaleResolution",
    "LocaleResolver",
    "detect_locale",
]
