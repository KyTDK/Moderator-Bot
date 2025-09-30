from __future__ import annotations

"""Discord app command translator bridging to the internal translation service."""

import asyncio
from typing import Any

from discord import Locale, app_commands

from .service import TranslationService

_LOCATION_LIMITS: dict[app_commands.TranslationContextLocation, int] = {
    app_commands.TranslationContextLocation.command_name: 32,
    app_commands.TranslationContextLocation.command_description: 100,
    app_commands.TranslationContextLocation.group_name: 32,
    app_commands.TranslationContextLocation.group_description: 100,
    app_commands.TranslationContextLocation.parameter_name: 32,
    app_commands.TranslationContextLocation.parameter_description: 100,
    app_commands.TranslationContextLocation.choice_name: 100,
}


def _apply_length_limit(
    value: str | None, context: app_commands.TranslationContext
) -> str | None:
    """Clamp *value* to the allowed Discord length for the given *context*."""

    if value is None:
        return None

    limit = _LOCATION_LIMITS.get(context.location)
    if limit is None or len(value) <= limit:
        return value

    # Truncate translations that exceed Discord's hard limits to avoid sync errors.
    return value[:limit]

class DiscordAppCommandTranslator(app_commands.Translator):
    """Adapter that resolves locale strings using :class:`TranslationService`."""

    def __init__(self, service: TranslationService) -> None:
        super().__init__()
        self._service = service

    async def load(self) -> None:
        # Ensure locale data is available before Discord starts requesting translations.
        repository = self._service.translator.repository
        await asyncio.to_thread(repository.ensure_loaded)

    async def translate(
        self,
        string: app_commands.locale_str,
        locale: Locale,
        context: app_commands.TranslationContext,
    ) -> str | None:
        extras = getattr(string, "extras", None) or {}
        key = extras.get("key")
        if not key:
            return None

        placeholders = extras.get("placeholders")
        fallback = extras.get("default", getattr(string, "message", None))
        translated = self._service.translate(
            key,
            locale=locale.value,
            placeholders=placeholders,
            fallback=fallback,
        )
        if isinstance(translated, str):
            return _apply_length_limit(translated, context)
        return translated

__all__ = ["DiscordAppCommandTranslator"]
