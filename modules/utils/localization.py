from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

TranslateFn = Callable[..., Any]


def localize_message(
    translator: TranslateFn | None,
    namespace: str,
    key: str,
    *,
    placeholders: Mapping[str, Any] | None = None,
    fallback: str,
    **translator_kwargs: Any,
) -> str:
    """Return a localized message or fall back to formatted text."""

    resolved_placeholders = dict(placeholders or {})
    formatted_fallback = fallback.format(**resolved_placeholders)
    if translator is None:
        return formatted_fallback
    return translator(
        f"{namespace}.{key}",
        placeholders=resolved_placeholders,
        fallback=formatted_fallback,
        **translator_kwargs,
    )


class LocalizedError(ValueError):
    """ValueError that carries translation metadata for user-facing messages."""

    def __init__(
        self,
        translation_key: str,
        fallback: str,
        *,
        placeholders: Mapping[str, Any] | None = None,
    ) -> None:
        self.translation_key = translation_key
        self.fallback = fallback
        self.placeholders = dict(placeholders or {})
        super().__init__(self._format_fallback())

    def _resolve_placeholders(
        self,
        translator: TranslateFn | None,
        translator_kwargs: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        resolved: dict[str, Any] = {}
        for key, value in self.placeholders.items():
            if callable(value):  # type: ignore[arg-type]
                resolved[key] = value(
                    translator=translator,
                    **(translator_kwargs or {}),
                )
            else:
                resolved[key] = value
        return resolved

    def _format_fallback(self) -> str:
        return self.fallback.format(**self._resolve_placeholders(None, None))

    def localize(
        self,
        translator: TranslateFn | None,
        **translator_kwargs: Any,
    ) -> str:
        fallback_message = self._format_fallback()
        if translator is None:
            return fallback_message
        resolved_placeholders = self._resolve_placeholders(
            translator,
            translator_kwargs,
        )
        return translator(
            self.translation_key,
            placeholders=resolved_placeholders,
            fallback=fallback_message,
            **translator_kwargs,
        )


__all__ = ["TranslateFn", "localize_message", "LocalizedError"]
