from discord import app_commands

ACTIONS = ["strike", "kick", "ban", "timeout", "delete", "give_role", "take_role"]

def action_choices() -> list[app_commands.Choice[str]]:
    """Return `app_commands.Choice` list for moderation actions."""
    return [app_commands.Choice(name=a, value=a) for a in ACTIONS]
