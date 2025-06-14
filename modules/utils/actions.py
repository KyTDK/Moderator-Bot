from typing import Iterable
from discord import app_commands

ACTIONS = [
    ("Strike", "strike"),
    ("Kick", "kick"),
    ("Ban", "ban"),
    ("Timeout", "timeout"),
    ("Delete Message", "delete"),
    ("Give Role", "give_role"),
    ("Remove Role", "take_role"),
]

VALID_ACTION_VALUES = [a[1] for a in ACTIONS]

def action_choices(exclude: Iterable[str] = ()) -> list[app_commands.Choice[str]]:
    exclude_set = set(exclude)
    return [
        app_commands.Choice(name=label, value=value)
        for label, value in ACTIONS
        if value not in exclude_set
    ]