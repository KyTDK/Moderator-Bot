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

        return normalized

    def push_locale(self, locale: Any | None) -> Token[str | None]:
        return _current_locale.set(normalise_locale(locale))

    def reset_locale(self, token: Token[str | None]) -> None:
        _current_locale.reset(token)

    @contextmanager
    def use_locale(self, locale: Any | None):
        token = self.push_locale(locale)
        try:
            yield
        finally:
            self.reset_locale(token)

    def current_locale(self) -> str | None:
        return _current_locale.get()


__all__ = ["TranslationService"]

