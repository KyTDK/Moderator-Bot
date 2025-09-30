from __future__ import annotations

import logging
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from .locale_utils import build_locale_chain
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
        chain = build_locale_chain(
            locale,
            default_locale=self._default_locale,
            fallback_locale=self._fallback_locale,
        )

        logger.info(
            "Translating key '%s' using locale '%s' (chain=%s)",
            key,
            locale or self._default_locale,
            chain,
        )

        for code in chain:
            logger.info(
                "Attempting to resolve key '%s' from locale '%s'", key, code
            )
            value = self._repository.get_value(code, key)
            if value is not None:
                if code != chain[0]:
                    logger.info("Translation for key '%s' satisfied by locale '%s'", key, code)
                if isinstance(value, str):
                    return self._apply_format(value, placeholders)
                return deepcopy(value)

        if fallback is not None:
            logger.warning(
                "Translation for key '%s' missing; using explicit fallback '%s'",
                key,
                fallback,
            )
            return self._apply_format(fallback, placeholders)

        logger.warning(
            "Translation for key '%s' missing; falling back to key with placeholders",
            key,
        )
        return self._apply_format(key, placeholders)

    def get_locale_snapshot(self, locale: str) -> dict[str, Any]:
        """Return a deep copy of the cached dictionary for *locale*."""

        return self._repository.get_locale_snapshot(locale)

    @staticmethod
    def _apply_format(template: str, placeholders: Mapping[str, Any]) -> str:
        if not placeholders:
            return Translator._normalize_escapes(template)
        try:
            formatted = template.format(**placeholders)
        except KeyError as exc:
            missing = exc.args[0]
            logger.warning("Missing placeholder '%s' for template '%s'", missing, template)
            return Translator._normalize_escapes(template)
        except ValueError:
            return Translator._normalize_escapes(template)
        return Translator._normalize_escapes(formatted)

    @staticmethod
    def _normalize_escapes(value: str) -> str:
        """Convert escaped newline sequences into actual newlines."""

        if "\\n" not in value:
            return value
        return value.replace("\\n", "\n")

__all__ = ["Translator"]
