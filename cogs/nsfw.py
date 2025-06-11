import discord
from discord.ext import commands
from discord import app_commands, Interaction

from modules.utils import mysql
from modules.utils.mysql import update_settings
from modules.utils.strike import validate_action_with_duration

NSFW_ACTION_SETTING = "nsfw-detection-action"

class NSFWCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    nsfw_group = app_commands.Group(
        name="nsfw",
        description="Manage NSFW content detection.",
        guild_only=True,
        default_permissions=discord.Permissions(manage_messages=True),
    )

    @nsfw_group.command(name="add_action", description="Add an action to the NSFW punishment list.")
    @app_commands.describe(
        action="Action: strike, kick, ban, timeout, delete",
        duration="Only required for timeout (e.g. 10m, 1h, 3d)"
    )
    @app_commands.choices(
        action=[
            app_commands.Choice(name="strike", value="strike"),
            app_commands.Choice(name="kick", value="kick"),
            app_commands.Choice(name="ban", value="ban"),
            app_commands.Choice(name="timeout", value="timeout"),
            app_commands.Choice(name="delete", value="delete")
        ])
    async def add_nsfw_action(
        self,
        interaction: Interaction,
        action: str,
        duration: str = None
    ):
        gid = interaction.guild.id

        action_str = await validate_action_with_duration(
            interaction=interaction,
            action=action,
            duration=duration,
            valid_actions=["strike", "kick", "ban", "timeout", "delete", "none"]
        )
        if action_str is None:
            return

        current = await mysql.get_settings(gid, NSFW_ACTION_SETTING) or []
        if not isinstance(current, list):
            current = [current] if current else []

        if action_str in current:
            await interaction.response.send_message(f"`{action_str}` is already in the action list.", ephemeral=True)
            return

        current.append(action_str)
        await update_settings(gid, NSFW_ACTION_SETTING, current)

        await interaction.response.send_message(f"Added `{action_str}` to NSFW actions.", ephemeral=True)

    @nsfw_group.command(name="remove_action", description="Remove an action from the NSFW punishment list.")
    @app_commands.describe(action="Exact action string to remove (e.g. timeout:1d, delete)")
    async def remove_nsfw_action(self, interaction: Interaction, action: str):
        gid = interaction.guild.id
        current = await mysql.get_settings(gid, NSFW_ACTION_SETTING) or []

        if action not in current:
            await interaction.response.send_message(f"`{action}` is not in the list.", ephemeral=True)
            return

        current.remove(action)
        await update_settings(gid, NSFW_ACTION_SETTING, current)

        await interaction.response.send_message(f"Removed `{action}` from NSFW actions.", ephemeral=True)

    @nsfw_group.command(name="view_actions", description="View the current list of NSFW punishment actions.")
    async def view_nsfw_actions(self, interaction: Interaction):
        gid = interaction.guild.id
        actions = await mysql.get_settings(gid, NSFW_ACTION_SETTING) or []

        if not actions:
            await interaction.response.send_message("No NSFW actions are currently set.", ephemeral=True)
            return

        if not isinstance(actions, list):
            actions = [actions]

        formatted = "\n".join(f"{i+1}. `{a}`" for i, a in enumerate(actions))
        await interaction.response.send_message(
            f"**Current NSFW actions:**\n{formatted}",
            ephemeral=True
        )

async def setup(bot: commands.Bot):
    await bot.add_cog(NSFWCog(bot))
