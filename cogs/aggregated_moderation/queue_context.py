from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Optional

_CURRENT_QUEUE_NAME: ContextVar[Optional[str]] = ContextVar("aggregated_moderation_queue", default=None)


def set_current_queue(name: Optional[str]) -> Token[Optional[str]]:
    return _CURRENT_QUEUE_NAME.set(name)


def reset_current_queue(token: Token[Optional[str]]) -> None:
    if token is None:
        return
    try:
        _CURRENT_QUEUE_NAME.reset(token)
    except ValueError:
        # Token already reset; ignore.
        pass


def get_current_queue() -> Optional[str]:
    return _CURRENT_QUEUE_NAME.get()


__all__ = ["get_current_queue", "reset_current_queue", "set_current_queue"]
