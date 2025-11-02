from __future__ import annotations

from typing import Any


def to_bool(value: Any, *, default: bool = False) -> bool:
    """Coerce arbitrary values to booleans consistently."""
    if value is None:
        return default
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return bool(value)


__all__ = ["to_bool"]
