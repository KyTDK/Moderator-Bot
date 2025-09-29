from __future__ import annotations

"""Runtime helpers for working with translations and locale context."""

import logging
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Any, Mapping

from .locale_utils import normalise_locale
from .translator import Translator

logger = logging.getLogger(__name__)

_current_locale: ContextVar[str | None] = ContextVar("moderator_bot_locale", default=None)


class TranslationService:
    """Wrap a :class:`Translator` with context-aware helpers."""

    def __init__(self, translator: Translator) -> None:
        self._translator = translator
        logger.debug(
            "TranslationService created with translator default=%s fallback=%s",
            translator.default_locale,
            translator.fallback_locale,
        )

    @property
    def translator(self) -> Translator:
        return self._translator

    def translate(
        self,
        key: str,
        *,
        locale: Any | None = None,
        placeholders: Mapping[str, Any] | None = None,
        fallback: str | None = None,
    ) -> Any:
        resolved_locale = self._prepare_locale(locale)
        logger.debug(
            "TranslationService.translate called (key=%s, requested_locale=%s, resolved_locale=%s, placeholders=%s, fallback=%s)",
            key,
            locale,
            resolved_locale,
            placeholders,
            fallback,
        )
        return self._translator.translate(
            key,
            locale=resolved_locale,
            placeholders=placeholders,
            fallback=fallback,
        )

    def _prepare_locale(self, locale: Any | None) -> str | None:
        if locale is None:
            current = _current_locale.get()
            logger.warning(
                "Translation requested but no locale was provided; using current context locale %r",
                current,
            )
            return current

        normalized = normalise_locale(locale)
        if normalized is None:
            current = _current_locale.get()
            logger.warning(
                "Translation requested but provided locale %r could not be normalised; using current context locale",
                locale,
            )
            return current

        logger.debug(
            "Locale prepared successfully (input=%r, normalized=%s)",
            locale,
            normalized,
        )
        return normalized

    def push_locale(self, locale: Any | None) -> Token[str | None]:
        normalized = normalise_locale(locale)
        logger.debug("Pushing locale onto context: %r -> %s", locale, normalized)
        return _current_locale.set(normalized)

    def reset_locale(self, token: Token[str | None]) -> None:
        logger.debug("Resetting locale context to previous value")
        _current_locale.reset(token)

    @contextmanager
    def use_locale(self, locale: Any | None):
        logger.debug("Entering locale context manager with locale=%s", locale)
        token = self.push_locale(locale)
        try:
            yield
        finally:
            self.reset_locale(token)
            logger.debug("Exited locale context manager (locale=%s)", locale)

    def current_locale(self) -> str | None:
        value = _current_locale.get()
        logger.debug("Current locale resolved to %s", value)
        return value


__all__ = ["TranslationService"]

