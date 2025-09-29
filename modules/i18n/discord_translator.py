from __future__ import annotations

"""Discord app command translator bridging to the internal translation service."""

from typing import Any

from discord import Locale, app_commands

from .service import TranslationService


class DiscordAppCommandTranslator(app_commands.Translator):
    """Adapter that resolves locale strings using :class:`TranslationService`."""

    def __init__(self, service: TranslationService) -> None:
        super().__init__()
        self._service = service

    async def load(self) -> None:
        # Nothing to preload; translations are read on-demand from the service.
        return

    async def translate(
        self,
        string: app_commands.locale_str,
        locale: Locale,
        context: app_commands.TranslationContext,
    ) -> str | None:
        key: str | None = getattr(string, "key", None)
        if not key:
            return None

        placeholders: dict[str, Any] | None = getattr(string, "placeholders", None)
        fallback = getattr(string, "default", getattr(string, "value", None))
        return self._service.translate(
            key,
            locale=locale.value,
            placeholders=placeholders,
            fallback=fallback,
        )


__all__ = ["DiscordAppCommandTranslator"]
