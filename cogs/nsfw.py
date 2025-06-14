import discord
from discord.ext import commands
from discord import app_commands, Interaction

from modules.utils.action_manager import ActionListManager
from modules.utils.strike import validate_action
from modules.utils.actions import action_choices, VALID_ACTION_VALUES

NSFW_ACTION_SETTING = "nsfw-detection-action"
manager = ActionListManager(NSFW_ACTION_SETTING)

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
        action="Action to perform",
        duration="Only required for timeout (e.g. 10m, 1h, 3d)"
    )
    @app_commands.choices(action=action_choices())
    async def add_nsfw_action(
        self,
        interaction: Interaction,
        action: str,
        duration: str = None,
        role: discord.Role = None
    ):
        await interaction.response.defer(ephemeral=True)
        
        gid = interaction.guild.id
        action_str = await validate_action(
            interaction=interaction,
            action=action,
            duration=duration,
            role=role,
            valid_actions=VALID_ACTION_VALUES,
        )
        if action_str is None:
            return

        message = await manager.add_action(gid, action_str)
        await interaction.response.send_message(message, ephemeral=True)

    @nsfw_group.command(name="remove_action", description="Remove an action from the NSFW punishment list.")
    @app_commands.describe(action="Exact action string to remove (e.g. timeout, delete)")
    @app_commands.choices(action=action_choices())
    async def remove_nsfw_action(self, interaction: Interaction, action: str):
        gid = interaction.guild.id

        message = await manager.remove_action(gid, action)
        await interaction.response.send_message(message, ephemeral=True)

    @nsfw_group.command(name="view_actions", description="View the current list of NSFW punishment actions.")
    async def view_nsfw_actions(self, interaction: Interaction):
        gid = interaction.guild.id

        actions = await manager.view_actions(gid)
        if not actions:
            await interaction.response.send_message("No NSFW actions are currently set.", ephemeral=True)
            return

        formatted = "\n".join(f"{i+1}. `{a}`" for i, a in enumerate(actions))
        await interaction.response.send_message(
            f"**Current NSFW actions:**\n{formatted}",
            ephemeral=True
        )

async def setup(bot: commands.Bot):
    await bot.add_cog(NSFWCog(bot))