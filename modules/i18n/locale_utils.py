"""Helpers for working with Moderator Bot locale codes."""

from __future__ import annotations

from typing import Any

import discord


def _build_locale_aliases() -> dict[str, str]:
    mapping: dict[str, str] = {}

    def register(canonical: str, *aliases: str) -> None:
        normalized_canonical = canonical.strip().replace("_", "-")
        if not normalized_canonical:
            return
        for candidate in (canonical, *aliases):
            normalized = candidate.strip().replace("_", "-")
            if not normalized:
                continue
            mapping[normalized.lower()] = normalized_canonical

    register("af-ZA", "af")
    register("ar-SA", "ar")
    register("bg")
    register("ca-ES", "ca")
    register("cs-CZ", "cs")
    register("da-DK", "da")
    register("de-DE", "de")
    register("el-GR", "el")
    register(
        "en",
        "en-US",
        "en-GB",
        "en-CA",
        "en-AU",
        "en-NZ",
        "en-IE",
        "en-IN",
        "en-ZA",
    )
    register("es-ES", "es", "es-419")
    register("fi-FI", "fi")
    register("fr-FR", "fr")
    register("he-IL", "he")
    register("hi")
    register("hr")
    register("hu-HU", "hu")
    register("id")
    register("it-IT", "it")
    register("ja-JP", "ja")
    register("ko-KR", "ko")
    register("lt")
    register("nl-NL", "nl")
    register("no-NO", "no")
    register("pl-PL", "pl")
    register("pt-PT", "pt")
    register("pt-BR")
    register("ro-RO", "ro")
    register("ru-RU", "ru")
    register("sk")
    register("sr-SP", "sr")
    register("sv-SE", "sv")
    register("th")
    register("tr-TR", "tr")
    register("uk-UA", "uk")
    register("vi-VN", "vi")
    register("zh-CN", "zh")
    register("zh-TW", "zh-HK")

    return mapping


SUPPORTED_LOCALE_ALIASES: dict[str, str] = _build_locale_aliases()


def normalise_locale(locale: Any) -> str | None:
    """Return the canonical locale for *locale* or ``None`` if unsupported."""

    if locale is None:
        return None

    if isinstance(locale, discord.Locale):
        raw = locale.value
    else:
        raw = str(locale)

    normalized = raw.strip().replace("_", "-")
    if not normalized:
        return None

    mapped = SUPPORTED_LOCALE_ALIASES.get(normalized.lower())
    return mapped


def list_supported_locales() -> list[str]:
    """Return the canonical locale codes supported by the bot."""

    return sorted({alias for alias in SUPPORTED_LOCALE_ALIASES.values()})


__all__ = [
    "SUPPORTED_LOCALE_ALIASES",
    "normalise_locale",
    "list_supported_locales",
]
