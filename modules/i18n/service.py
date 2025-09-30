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
        logger.info(
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
        return self._translator.translate(
            key,
            locale=resolved_locale,
            placeholders=placeholders,
            fallback=fallback,
        )

    def _resolve_context_locale(self) -> str:
        current = _current_locale.get()
        if current:
            return current

        default_locale = self._translator.default_locale
        logger.info(
            "No locale currently bound to context; using translator default '%s'",
            default_locale,
        )
        return default_locale

    def _prepare_locale(self, locale: Any | None) -> str:
        if locale is None:
            context_locale = self._resolve_context_locale()
            logger.info(
                "Translation requested without explicit locale; using context locale '%s'",
                context_locale,
            )
            return context_locale

        normalized = normalise_locale(locale)
        if normalized is None:
            context_locale = self._resolve_context_locale()
            logger.warning(
                "Translation requested but provided locale %r could not be normalised; using context locale '%s'",
                locale,
                context_locale,
            )
            return context_locale

        logger.info(
            "Locale prepared successfully (input=%r, normalized=%s)",
            locale,
            normalized,
        )
        return normalized

    def push_locale(self, locale: Any | None) -> Token[str | None]:
        normalized = normalise_locale(locale)
        if normalized is None and locale is not None:
            logger.warning(
                "Attempted to push invalid locale %r onto context; using default '%s' instead",
                locale,
                self._translator.default_locale,
            )
        resolved = normalized or self._translator.default_locale
        logger.info("Pushing locale onto context: %r -> %s", locale, resolved)
        return _current_locale.set(resolved)

    def reset_locale(self, token: Token[str | None]) -> None:
        logger.info("Resetting locale context to previous value")
        _current_locale.reset(token)

    @contextmanager
    def use_locale(self, locale: Any | None):
        logger.info("Entering locale context manager with locale=%s", locale)
        token = self.push_locale(locale)
        try:
            yield
        finally:
            self.reset_locale(token)
            logger.info("Exited locale context manager (locale=%s)", locale)

    def current_locale(self) -> str | None:
        value = _current_locale.get()
        logger.info("Current locale resolved to %s", value)
        return value


__all__ = ["TranslationService"]

