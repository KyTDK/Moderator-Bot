"""Helpers for emitting consistent i18n logging messages."""

from __future__ import annotations

from difflib import get_close_matches
from typing import Iterable


def suggest_locale(target: str, available: Iterable[str]) -> str | None:
    """Return the closest matching locale code for *target*.

    Parameters
    ----------
    target:
        The locale code that was requested by the caller.
    available:
        An iterable of available locale identifiers.

    Returns
    -------
    Optional[str]
        The closest matching locale code, if a sufficiently similar candidate
        exists. The similarity threshold mirrors :func:`difflib.get_close_matches`.
    """

    matches = get_close_matches(target, list(available), n=1, cutoff=0.6)
    return matches[0] if matches else None


def format_missing_locale_message(
    requested: str,
    fallback: str,
    available: Iterable[str],
) -> str:
    """Return a human-readable warning for a missing locale.

    The message includes the configured fallback locale and, when possible, a
    "did you mean" suggestion for the closest known locale identifier.
    """

    suggestion = suggest_locale(requested, available)
    suggestion_hint = f" Did you mean '{suggestion}'?" if suggestion else ""
    return (
        "Requested locale '%s' missing from cache; using fallback '%s'.%s"
        % (requested, fallback, suggestion_hint)
    )


__all__ = ["format_missing_locale_message", "suggest_locale"]
