from __future__ import annotations

from typing import Any, Iterable

TEXT_SOURCE_MESSAGES = "messages"
TEXT_SOURCE_OCR = "ocr"
DEFAULT_TEXT_SOURCES = (TEXT_SOURCE_MESSAGES, TEXT_SOURCE_OCR)
_ALLOWED_SOURCES = {TEXT_SOURCE_MESSAGES, TEXT_SOURCE_OCR}


def normalize_text_sources(value: Any | None) -> tuple[str, ...]:
    if value is None:
        return DEFAULT_TEXT_SOURCES

    if isinstance(value, str):
        candidates: Iterable[Any] = [value]
    elif isinstance(value, (list, tuple, set)):
        candidates = value
    else:
        return DEFAULT_TEXT_SOURCES

    normalized: list[str] = []
    seen: set[str] = set()
    for entry in candidates:
        if entry is None:
            continue
        key = str(entry).strip().lower()
        if key in _ALLOWED_SOURCES and key not in seen:
            normalized.append(key)
            seen.add(key)

    return tuple(normalized or DEFAULT_TEXT_SOURCES)


__all__ = [
    "DEFAULT_TEXT_SOURCES",
    "TEXT_SOURCE_MESSAGES",
    "TEXT_SOURCE_OCR",
    "normalize_text_sources",
]
