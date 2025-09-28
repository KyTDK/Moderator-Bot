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
    )


__all__ = ["TranslateFn", "localize_message"]
