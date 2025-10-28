from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class ActionSpec:
    """Metadata describing the requirements for a disciplinary action."""

    canonical_name: str
    allows_duration: bool = False
    requires_duration: bool = False
    allows_role: bool = False
    requires_role: bool = False
    requires_message: bool = False
    missing_message_key: str | None = None
    missing_message_fallback: str | None = None


_ACTION_SPECS: Mapping[str, ActionSpec] = {
    "none": ActionSpec(canonical_name="none"),
    "delete": ActionSpec(canonical_name="delete"),
    "strike": ActionSpec(canonical_name="strike", allows_duration=True),
    "kick": ActionSpec(canonical_name="kick"),
    "ban": ActionSpec(canonical_name="ban"),
    "timeout": ActionSpec(
        canonical_name="timeout",
        allows_duration=True,
        requires_duration=True,
    ),
    "give_role": ActionSpec(
        canonical_name="give_role",
        allows_role=True,
        requires_role=True,
    ),
    "take_role": ActionSpec(
        canonical_name="take_role",
        allows_role=True,
        requires_role=True,
    ),
    "remove_role": ActionSpec(
        canonical_name="take_role",
        allows_role=True,
        requires_role=True,
    ),
    "warn": ActionSpec(
        canonical_name="warn",
        requires_message=True,
        missing_message_key="warn_message_required",
        missing_message_fallback="You must provide a warning message.",
    ),
    "broadcast": ActionSpec(
        canonical_name="broadcast",
        requires_message=True,
        missing_message_key="broadcast_message_required",
        missing_message_fallback="You must provide a broadcast message.",
    ),
}


ROLE_ACTION_ALIASES: frozenset[str] = frozenset(
    name for name, spec in _ACTION_SPECS.items() if spec.requires_role
)
"""Action names that operate on roles."""

ROLE_ACTION_CANONICAL: frozenset[str] = frozenset(
    spec.canonical_name for spec in _ACTION_SPECS.values() if spec.requires_role
)
"""Canonical role action identifiers."""


def get_action_spec(action_name: str) -> ActionSpec | None:
    """Return the :class:`ActionSpec` for *action_name* if known."""

    return _ACTION_SPECS.get(action_name)
