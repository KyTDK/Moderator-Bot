from __future__ import annotations

"""High-level helpers for working with translated mappings."""

from collections.abc import Mapping
from typing import Any


def get_translated_mapping(
    bot: Any,
    key: str,
    fallback: Mapping[str, Any],
    *,
    guild_id: int | None = None,
    locale: str | None = None,
    placeholders: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the translated mapping for *key* with *fallback* defaults.

    Parameters
    ----------
    bot:
        Any object exposing a ``translate`` method compatible with
        :meth:`modules.core.moderator_bot.ModeratorBot.translate`.
    key:
        The translation key to resolve.
    fallback:
        Mapping of default values that will be merged with any translated
        entries. The returned dictionary is always a shallow copy so callers can
        mutate it safely.
    guild_id / locale / placeholders:
        Optional keyword arguments forwarded to the translator when available.

    Returns
    -------
    dict[str, Any]
        A copy of *fallback* updated with any translated entries. When no
        translator is available, the fallback copy is returned unchanged.
    """

    merged: dict[str, Any] = dict(fallback)
    translator = getattr(bot, "translate", None)
    if not callable(translator):
        return merged

    translated = translator(
        key,
        guild_id=guild_id,
        locale=locale,
        placeholders=placeholders,
    )
    if isinstance(translated, Mapping):
        for translated_key, translated_value in translated.items():
            if isinstance(translated_key, str):
                merged[translated_key] = translated_value
    return merged


__all__ = ["get_translated_mapping"]
