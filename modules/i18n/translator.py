from __future__ import annotations

import logging
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from .locale_utils import normalise_locale
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

        logger.debug(
            "Translating key '%s' using locale '%s' (chain=%s)",
            key,
            locale or self._default_locale,
            locales_to_try,
        )

        for locale_code in locales_to_try:
            value = self._repository.get_value(locale_code, key)
            if value is not None:
                if locale_code != locales_to_try[0]:
                    logger.debug(
                        "Translation for key '%s' satisfied by locale '%s'", key, locale_code
                    )
                return self._format_value(value, placeholders)

        if fallback is not None:
            logger.debug(
                "Translation for key '%s' missing; using explicit fallback '%s'",
                key,
                fallback,
            )
            return self._apply_format(fallback, placeholders)

        logger.debug(
            "Translation for key '%s' missing; falling back to key with placeholders", key
        )
        return self._apply_format(key, placeholders)

    def get_locale_snapshot(self, locale: str) -> dict[str, Any]:
        """Return a deep copy of the cached dictionary for *locale*."""

        return self._repository.get_locale_snapshot(locale)

    def _build_locale_chain(self, locale: str | None) -> list[str]:
        chain: list[str] = []
        if locale:
            sanitized = locale.replace("_", "-")

            canonical = normalise_locale(sanitized)
            if canonical:
                chain.append(canonical)
                normalized = canonical
                if canonical.lower() != sanitized.lower():
                    chain.append(sanitized)
            else:
                chain.append(sanitized)
                normalized = sanitized

            if "-" in normalized:
                base = normalized.split("-", 1)[0]
                base_canonical = normalise_locale(base)
                chain.append(base_canonical or base)
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
