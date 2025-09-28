from __future__ import annotations

import logging
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from .locales import LocaleRepository

logger = logging.getLogger(__name__)

class Translator:
    """Lightweight accessor for translated strings with locale fallbacks."""

    def __init__(
        self,
        repository: LocaleRepository,
        *,
        default_locale: str | None = None,
        fallback_locale: str | None = None,
    ) -> None:
        self._repository = repository
        self._default_locale = default_locale or repository.default_locale
        self._fallback_locale = fallback_locale or repository.fallback_locale

    @property
    def repository(self) -> LocaleRepository:
        return self._repository

    @property
    def default_locale(self) -> str:
        return self._default_locale

    @property
    def fallback_locale(self) -> str:
        return self._fallback_locale

    def translate(
        self,
        key: str,
        *,
        locale: str | None = None,
        placeholders: Mapping[str, Any] | None = None,
        fallback: str | None = None,
    ) -> Any:
        """Return the translated value for *key* with optional formatting."""

        placeholders = placeholders or {}
        locales_to_try = self._build_locale_chain(locale)

        for locale_code in locales_to_try:
            value = self._repository.get_value(locale_code, key)
            if value is not None:
                return self._format_value(value, placeholders)

        if fallback is not None:
            return self._apply_format(fallback, placeholders)

        return self._apply_format(key, placeholders)

    def get_locale_snapshot(self, locale: str) -> dict[str, Any]:
        """Return a deep copy of the cached dictionary for *locale*."""

        return self._repository.get_locale_snapshot(locale)

    def _build_locale_chain(self, locale: str | None) -> list[str]:
        chain: list[str] = []
        if locale:
            chain.append(locale)
            normalized = locale.replace("_", "-")
            if "-" in normalized:
                base = normalized.split("-", 1)[0]
                chain.append(base)
        chain.extend([self._default_locale, self._fallback_locale])
        # Ensure we only keep the first occurrence of each locale code
        seen: set[str] = set()
        unique_chain: list[str] = []
        for code in chain:
            if code not in seen:
                unique_chain.append(code)
                seen.add(code)
        return unique_chain

    def _format_value(self, value: Any, placeholders: Mapping[str, Any]) -> Any:
        if isinstance(value, str):
            return self._apply_format(value, placeholders)
        if isinstance(value, Mapping):
            return {key: self._format_value(val, placeholders) for key, val in value.items()}
        if isinstance(value, list):
            return [self._format_value(item, placeholders) for item in value]
        return deepcopy(value)

    @staticmethod
    def _apply_format(template: str, placeholders: Mapping[str, Any]) -> str:
        if not placeholders:
            return template
        try:
            return template.format(**placeholders)
        except KeyError as exc:
            missing = exc.args[0]
            logger.warning("Missing placeholder '%s' for template '%s'", missing, template)
            return template
        except ValueError:
            # Invalid format string; return template unchanged.
            return template

__all__ = ["Translator"]
