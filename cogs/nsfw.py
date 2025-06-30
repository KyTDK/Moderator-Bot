import discord
from discord.ext import commands
from discord import app_commands, Interaction

from modules.utils.action_manager import ActionListManager
from modules.utils.list_manager import ListManager
from modules.utils.strike import validate_action
from modules.utils.actions import action_choices, VALID_ACTION_VALUES

NSFW_ACTION_SETTING = "nsfw-detection-action"
manager = ActionListManager(NSFW_ACTION_SETTING)
NSFW_CATEGORY_SETTING = "nsfw-detection-categories"
category_manager = ListManager(NSFW_CATEGORY_SETTING)

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
        role: discord.Role = None,
        reason: str = None,
    ):
        await interaction.response.defer(ephemeral=True)
        
        gid = interaction.guild.id
        action_str = await validate_action(
            interaction=interaction,
            action=action,
            duration=duration,
            role=role,
            valid_actions=VALID_ACTION_VALUES,
            param=reason,
        )
        if action_str is None:
            return

        message = await manager.add_action(gid, action_str)
        await interaction.followup.send(message, ephemeral=True)

    @nsfw_group.command(name="remove_action", description="Remove an action from the NSFW punishment list.")
    @app_commands.describe(action="Exact action string to remove (e.g. timeout, delete)")
    @app_commands.autocomplete(action=manager.autocomplete)
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

    @nsfw_group.command(name="add_category", description="Add a category to NSFW detection.")
    async def add_category(self, interaction: Interaction, category: str):
        gid = interaction.guild.id
        message = await category_manager.add(gid, category)
        await interaction.response.send_message(message, ephemeral=True)

    @nsfw_group.command(name="remove_category", description="Remove a category from NSFW detection.")
    @app_commands.autocomplete(category=category_manager.autocomplete)
    async def remove_category(self, interaction: Interaction, category: str):
        gid = interaction.guild.id
        message = await category_manager.remove(gid, category)
        await interaction.response.send_message(message, ephemeral=True)

    @nsfw_group.command(name="view_categories", description="View NSFW detection categories.")
    async def view_categories(self, interaction: Interaction):
        gid = interaction.guild.id
        categories = await category_manager.view(gid)
        if not categories:
            await interaction.response.send_message("No categories are currently set.", ephemeral=True)
            return
        formatted = "\n".join(f"{i+1}. `{c}`" for i, c in enumerate(categories))
        await interaction.response.send_message(
            f"**Current NSFW categories:**\n{formatted}",
            ephemeral=True
        )

async def setup(bot: commands.Bot):
    await bot.add_cog(NSFWCog(bot))