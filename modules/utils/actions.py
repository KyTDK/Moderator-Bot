from typing import Iterable, Optional, Union
from discord import app_commands

ACTIONS = [
    ("Strike", "strike"),
    ("Kick", "kick"),
    ("Ban", "ban"),
    ("Timeout", "timeout"),
    ("Delete Message", "delete"),
    ("Give Role", "give_role"),
    ("Remove Role", "take_role"),
    ("Warn User", "warn")
    ("Broadcast Message", "broadcast")
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
                base.append((item.capitalize(), item))
            else:
                base.append(item)

    seen = set()
    final = []
    for label, value in base:
        if value not in seen:
            final.append(app_commands.Choice(name=label, value=value))
            seen.add(value)

    return final