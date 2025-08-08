import discord
from discord.ext import commands
from discord import app_commands, Interaction

from modules.utils import mysql
from modules.utils.action_manager import ActionListManager
from modules.utils.discord_utils import require_accelerated
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
    @app_commands.choices(
        category=[
            app_commands.Choice(name="Violence Graphic", value="violence_graphic"),
            app_commands.Choice(name="Violence", value="violence"),
            app_commands.Choice(name="Sexual", value="sexual"),
            app_commands.Choice(name="Self Harm Instructions", value="self_harm_instructions"),
            app_commands.Choice(name="Self Harm Intent", value="self_harm_intent"),
            app_commands.Choice(name="Self Harm", value="self_harm"),
        ]
    )
    async def add_category(self, interaction: Interaction, category: str):
        # Accelerated only
        if not await require_accelerated(interaction):
            return
        # Continue with adding category
        gid = interaction.guild.id
        message = await category_manager.add(gid, category)
        await interaction.response.send_message(message, ephemeral=True)

    @nsfw_group.command(name="remove_category", description="Remove a category from NSFW detection.")
    @app_commands.autocomplete(category=category_manager.autocomplete)
    async def remove_category(self, interaction: Interaction, category: str):
        # Accelerated only
        if not await require_accelerated(interaction):
            return
        # Continue with adding category
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

    @nsfw_group.command(name="set_threshold", description="Set the threshold for NSFW detection confidence.")
    @app_commands.describe(threshold="Confidence threshold (0.0 to 1.0)")
    async def set_threshold(self, interaction: Interaction, threshold: float):
        if not (0.0 <= threshold <= 1.0):
            await interaction.response.send_message(
                "Threshold must be between 0.0 and 1.0.",
                ephemeral=True
            )
            return

        gid = interaction.guild.id
        await mysql.update_settings(gid, "threshold", threshold)
        await interaction.response.send_message(
            f"NSFW detection threshold set to {threshold:.2f}.",
            ephemeral=True
        )

    @nsfw_group.command(name="view_threshold", description="View the current NSFW detection threshold.")
    async def view_threshold(self, interaction: Interaction):
        gid = interaction.guild.id
        threshold = await mysql.get_settings(gid, "threshold")
        if threshold is None:
            await interaction.response.send_message(
                "NSFW detection threshold is not set.",
                ephemeral=True
            )
            return

        await interaction.response.send_message(
            f"Current NSFW detection threshold: {threshold:.2f}",
            ephemeral=True
        )

async def setup(bot: commands.Bot):
    await bot.add_cog(NSFWCog(bot))