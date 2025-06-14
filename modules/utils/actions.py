from discord import app_commands

BASIC_ACTIONS = ["strike", "kick", "ban", "timeout", "delete"]
ROLE_ACTIONS = ["give_role", "take_role"]
ALL_ACTIONS = BASIC_ACTIONS + ROLE_ACTIONS


def action_choices(include_role_actions: bool = True) -> list[app_commands.Choice[str]]:
    """Return `app_commands.Choice` list for moderation actions."""
    actions = ALL_ACTIONS if include_role_actions else BASIC_ACTIONS
    return [app_commands.Choice(name=a, value=a) for a in actions]
