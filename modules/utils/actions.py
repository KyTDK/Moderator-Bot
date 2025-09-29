from typing import Iterable, Optional, Union
from discord import app_commands


def _choice_label(fallback: str, value: str) -> app_commands.locale_str:
    return app_commands.locale_str(
        fallback,
        key=f"modules.utils.actions.choices.{value}",
    )


ACTIONS = [
    (_choice_label("Strike", "strike"), "strike"),
    (_choice_label("Kick", "kick"), "kick"),
    (_choice_label("Ban", "ban"), "ban"),
    (_choice_label("Timeout", "timeout"), "timeout"),
    (_choice_label("Delete Message", "delete"), "delete"),
    (_choice_label("Give Role", "give_role"), "give_role"),
    (_choice_label("Remove Role", "take_role"), "take_role"),
    (_choice_label("Warn User", "warn"), "warn"),
    (_choice_label("Broadcast Message", "broadcast"), "broadcast"),
]

VALID_ACTION_VALUES = [a[1] for a in ACTIONS]

def action_choices(
    exclude: Iterable[str] = (),
    include: Optional[Union[Iterable[str], Iterable[tuple[str, str]]]] = None
) -> list[app_commands.Choice[str]]:
    exclude_set = set(exclude)

    base = [(label, value) for label, value in ACTIONS if value not in exclude_set]

    if include:
        for item in include:
            if isinstance(item, str):
                base.append((_choice_label(item.capitalize(), item), item))
            else:
                label, value = item
                base.append((_choice_label(label, value), value))

    seen = set()
    final = []
    for label, value in base:
        if value not in seen:
            final.append(app_commands.Choice(name=label, value=value))
            seen.add(value)

    return final