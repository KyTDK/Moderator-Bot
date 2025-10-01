from __future__ import annotations

from typing import Iterable, Optional, Union

from discord import app_commands

from modules.i18n.strings import locale_namespace

CHOICE_LABELS = locale_namespace("modules", "utils", "actions", "choices")


def _choice_label(value: str, *, fallback: str | None = None) -> app_commands.locale_str:
    extras: dict[str, str] = {}
    if fallback is not None:
        extras["default"] = fallback
    return CHOICE_LABELS.string(value, **extras)


ACTIONS = [
    (_choice_label("strike"), "strike"),
    (_choice_label("kick"), "kick"),
    (_choice_label("ban"), "ban"),
    (_choice_label("timeout"), "timeout"),
    (_choice_label("delete"), "delete"),
    (_choice_label("give_role"), "give_role"),
    (_choice_label("take_role"), "take_role"),
    (_choice_label("warn"), "warn"),
    (_choice_label("broadcast"), "broadcast"),
]

VALID_ACTION_VALUES = [a[1] for a in ACTIONS]


def action_choices(
    exclude: Iterable[str] = (),
    include: Optional[Union[Iterable[str], Iterable[tuple[str, str]]]] = None,
) -> list[app_commands.Choice[str]]:
    exclude_set = set(exclude)

    base = [(label, value) for label, value in ACTIONS if value not in exclude_set]

    if include:
        for item in include:
            if isinstance(item, str):
                base.append((_choice_label(item, fallback=item.capitalize()), item))
            else:
                label, value = item
                base.append((_choice_label(value, fallback=label), value))

    seen = set()
    final = []
    for label, value in base:
        if value not in seen:
            final.append(app_commands.Choice(name=label, value=value))
            seen.add(value)

    return final
