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


def _normalise_input(value: Any) -> str | None:
    """Return a normalised representation of *value* suitable for lookups."""

    if value is None:
        return None

    if isinstance(value, discord.Locale):
        raw = value.value
    else:
        raw = str(value)

    normalised = raw.strip().replace("_", "-")
    return normalised or None


def _push_unique(values: list[str], seen: set[str], candidate: Any) -> None:
    normalised = _normalise_input(candidate)
    if not normalised:
        return

    key = normalised.lower()
    if key in seen:
        return

    values.append(normalised)
    seen.add(key)


def normalise_locale(locale: Any) -> str | None:
    """Return the canonical locale for *locale* or ``None`` if unsupported."""

    normalised = _normalise_input(locale)
    if not normalised:
        return None

    return SUPPORTED_LOCALE_ALIASES.get(normalised.lower())


def build_locale_chain(
    locale: Any | None,
    *,
    default_locale: str,
    fallback_locale: str,
) -> list[str]:
    """Return the ordered lookup chain for *locale*.

    The resulting chain always includes ``default_locale`` and ``fallback_locale``
    (if different) and ensures base language fallbacks (``xx-XX`` → ``xx``)
    are attempted before falling back to English.
    """

    ordered: list[str] = []
    seen: set[str] = set()

    normalised = _normalise_input(locale)
    canonical = normalise_locale(normalised) if normalised else None

    if canonical:
        _push_unique(ordered, seen, canonical)

    if normalised and (canonical is None or normalised.lower() != canonical.lower()):
        _push_unique(ordered, seen, normalised)

    reference = canonical or normalised
    if reference and "-" in reference:
        base = reference.split("-", 1)[0]
        _push_unique(ordered, seen, base)

        base_canonical = normalise_locale(base)
        if base_canonical:
            _push_unique(ordered, seen, base_canonical)

    _push_unique(ordered, seen, default_locale)
    _push_unique(ordered, seen, fallback_locale)

    return ordered


def list_supported_locales() -> list[str]:
    """Return the canonical locale codes supported by the bot."""

    return sorted({alias for alias in SUPPORTED_LOCALE_ALIASES.values()})


__all__ = [
    "SUPPORTED_LOCALE_ALIASES",
    "normalise_locale",
    "build_locale_chain",
    "list_supported_locales",
]
