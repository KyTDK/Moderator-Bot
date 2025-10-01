"""Utilities for referencing locale strings consistently across the codebase."""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from discord import app_commands

__all__ = [
    "LocaleNamespace",
    "locale_key",
    "locale_value",
    "locale_string",
    "locale_namespace",
]


def _iter_segments(parts: Iterable[str]) -> Iterable[str]:
    for part in parts:
        if not part:
            continue
        for segment in part.split('.'):
            segment = segment.strip()
            if segment:
                yield segment


def locale_key(*parts: str) -> str:
    """Return a normalised locale key composed from *parts*."""

    return '.'.join(_iter_segments(parts))


def _deep_merge(base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


@lru_cache(maxsize=None)
def _load_locale(locale: str) -> dict[str, Any]:
    """Load the bundled fallback locale for resolving command metadata."""

    root = Path(__file__).resolve().parents[2] / "locales" / locale
    if not root.exists():
        return {}

    data: dict[str, Any] = {}
    for path in sorted(root.rglob("*.json")):
        try:
            with path.open("r", encoding="utf-8-sig") as handle:
                payload = json.load(handle)
        except FileNotFoundError:
            continue
        except json.JSONDecodeError as exc:  # pragma: no cover - defensive
            raise ValueError(f"Failed to parse locale file {path}: {exc}") from exc
        if isinstance(payload, dict):
            data = _deep_merge(data, payload)
    return data


def locale_value(key: str, *, locale: str = "en") -> Any:
    """Return the fallback value stored in the bundled *locale* for *key*."""

    cursor: Any = _load_locale(locale)
    for segment in locale_key(key).split('.'):
        if not segment:
            continue
        if isinstance(cursor, dict) and segment in cursor:
            cursor = cursor[segment]
        else:
            raise KeyError(key)
    return cursor


def locale_string(*parts: str, fallback_locale: str = "en", **extras: Any) -> app_commands.locale_str:
    """Create an :func:`discord.app_commands.locale_str` with automatic fallback."""

    key = extras.pop("key", None)
    if key is None:
        key = locale_key(*parts)
    fallback = extras.pop("default", None)
    if fallback is None:
        try:
            value = locale_value(key, locale=fallback_locale)
        except KeyError:
            value = key
    else:
        value = fallback
    return app_commands.locale_str(value, key=key, **extras)


@dataclass(frozen=True)
class LocaleNamespace:
    """Helper for building locale keys with a shared prefix."""

    parts: tuple[str, ...]

    def key(self, *parts: str) -> str:
        return locale_key(*self.parts, *parts)

    def value(self, *parts: str, fallback_locale: str = "en") -> Any:
        key = self.key(*parts)
        return locale_value(key, locale=fallback_locale)

    def string(self, *parts: str, fallback_locale: str = "en", **extras: Any) -> app_commands.locale_str:
        return locale_string(*self.parts, *parts, fallback_locale=fallback_locale, **extras)

    def child(self, *parts: str) -> "LocaleNamespace":
        return LocaleNamespace(self.parts + tuple(_iter_segments(parts)))


def locale_namespace(*parts: str) -> LocaleNamespace:
    return LocaleNamespace(tuple(_iter_segments(parts)))
