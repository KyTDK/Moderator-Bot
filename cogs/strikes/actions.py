from __future__ import annotations

import discord
from discord import Interaction

from modules.utils import mysql
from modules.utils.actions import VALID_ACTION_VALUES
from modules.utils.strike import validate_action


async def add_action_command(
    cog,
    interaction: Interaction,
    number_of_strikes: int,
    action: str,
    duration: str | None,
    role: discord.Role | None,
    channel: discord.TextChannel | None,
    reason: str | None,
) -> None:
    await interaction.response.defer(ephemeral=True)
    guild_id = interaction.guild.id
    strike_actions = await mysql.get_settings(guild_id, "strike-actions") or {}
    key = str(number_of_strikes)
    action_str = await validate_action(
        interaction=interaction,
        action=action,
        duration=duration,
        role=role,
        channel=channel,
        valid_actions=VALID_ACTION_VALUES,
        param=reason,
        translator=cog.bot.translate,
    )
    if action_str is None:
        return

    texts = cog.bot.translate("cogs.strikes.actions", guild_id=guild_id)
    actions_list = strike_actions.get(key, [])
    if action_str in actions_list:
        await interaction.followup.send(
            texts["exists"].format(action=action_str, key=key),
            ephemeral=True,
        )
        return

    actions_list.append(action_str)
    strike_actions[key] = actions_list
    await mysql.update_settings(guild_id, "strike-actions", strike_actions)
    await interaction.followup.send(
        texts["added"].format(action=action_str, key=key),
        ephemeral=True,
    )


async def remove_action_command(
    cog,
    interaction: Interaction,
    number_of_strikes: int,
    action: str,
) -> None:
    await interaction.response.defer(ephemeral=True)
    guild_id = interaction.guild.id
    strike_actions = await mysql.get_settings(guild_id, "strike-actions") or {}
    key = str(number_of_strikes)
    actions_list = strike_actions.get(key)
    texts = cog.bot.translate("cogs.strikes.actions", guild_id=guild_id)
    if not actions_list or action not in actions_list:
        await interaction.followup.send(
            texts["missing"].format(action=action, key=key),
            ephemeral=True,
        )
        return

    actions_list.remove(action)
    if actions_list:
        strike_actions[key] = actions_list
    else:
        strike_actions.pop(key)
    await mysql.update_settings(guild_id, "strike-actions", strike_actions)
    await interaction.followup.send(
        texts["removed"].format(action=action, key=key),
        ephemeral=True,
    )


async def view_actions_command(
    cog,
    interaction: Interaction,
) -> None:
    guild_id = interaction.guild.id
    actions_texts = cog.bot.translate("cogs.strikes.view_actions", guild_id=guild_id)
    strike_actions = await mysql.get_settings(guild_id, "strike-actions") or {}
    if not strike_actions:
        await interaction.response.send_message(actions_texts["none"], ephemeral=True)
        return

    lines = []
    for key in sorted(strike_actions.keys(), key=int):
        actions = ", ".join(strike_actions[key])
        lines.append(actions_texts["item"].format(key=key, actions=actions))
    await interaction.response.send_message(
        actions_texts["heading"] + "\n" + "\n".join(lines),
        ephemeral=True,
    )


__all__ = [
    "add_action_command",
    "remove_action_command",
    "view_actions_command",
]
