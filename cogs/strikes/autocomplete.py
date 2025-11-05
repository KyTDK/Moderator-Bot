from __future__ import annotations

from discord import app_commands, Interaction

from modules.utils import mysql


async def autocomplete_strike_action(
    interaction: Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    settings = await mysql.get_settings(interaction.guild.id, "strike-actions") or {}
    all_actions: set[str] = set()
    for action_list in settings.values():
        all_actions.update(action_list)
    return [
        app_commands.Choice(name=action, value=action)
        for action in sorted(all_actions)
        if current.lower() in action.lower()
    ][:25]


__all__ = ["autocomplete_strike_action"]
