from discord.ext import commands
from discord import app_commands, Interaction
import discord
from modules.utils import mysql
from modules.config.settings_schema import SETTINGS_SCHEMA

class Settings(commands.Cog):
    """A cog for settings commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # List to hold choices for non-channel settings
    non_channel_choices = [
        app_commands.Choice(name=setting.name, value=setting_name)
        for setting_name, setting in SETTINGS_SCHEMA.items()
        if setting.type != discord.TextChannel
    ]

    # List to hold choices for channel settings
    channel_choices = [
        app_commands.Choice(name=setting.name, value=setting_name)
        for setting_name, setting in SETTINGS_SCHEMA.items()
        if setting.type == discord.TextChannel
    ]
    
    @app_commands.command(name="remove_setting", description="Remove a server setting.")
    @app_commands.choices(name=non_channel_choices+channel_choices)
    @app_commands.checks.has_permissions(moderate_members=True)
    async def remove_setting(self, interaction: Interaction, name: str):
        """Remove a server setting."""
        schema = SETTINGS_SCHEMA.get(name)
        if not schema:
            await interaction.response.send_message(
                f"Invalid setting name. Available settings: {', '.join(SETTINGS_SCHEMA.keys())}",
                ephemeral=True,
            )
            return

        # Remove the setting from the database
        mysql.update_settings(interaction.guild.id, name, None)
        await interaction.response.send_message(
            f"Removed `{name}` setting.", ephemeral=True
        )


    @app_commands.command(name="set_setting", description="Set a server setting.")
    @app_commands.choices(name=non_channel_choices)
    @app_commands.checks.has_permissions(moderate_members=True)
    async def set_setting(self, interaction: Interaction, name: str, value: str):
        schema = SETTINGS_SCHEMA.get(name)
        if not schema:
            await interaction.response.send_message(
                f"Invalid setting name. Available settings: {', '.join(SETTINGS_SCHEMA.keys())}",
                ephemeral=True,
            )
            return

        try:
            # Parse value based on expected type
            if schema.type == int:
                parsed_value = int(value)
            elif schema.type == bool:
                parsed_value = value.lower() in ["true", "1", "yes"]
            elif schema.type == discord.TextChannel:
                raise ValueError("This setting requires a channel, use `/set_channel` instead.")
            else:
                parsed_value = value

            if not schema.validate(parsed_value):
                raise ValueError("Value failed custom validation.")

            mysql.update_settings(interaction.guild.id, name, parsed_value)
            await interaction.response.send_message(
                f"Updated `{name}` to `{value}`.", ephemeral=True
            )

        except Exception as e:
            await interaction.response.send_message(
                f"Failed to update setting: {e}", ephemeral=True
            )

    @app_commands.command(name="set_channel", description="Set a channel for a setting.")
    @app_commands.choices(name=channel_choices)
    @app_commands.checks.has_permissions(moderate_members=True)
    async def set_channel(self, interaction: Interaction, name: str, channel: discord.TextChannel):
        schema = SETTINGS_SCHEMA.get(name)
        if not schema or schema.type != discord.TextChannel:
            await interaction.response.send_message(
                f"Invalid setting name or type. Available settings: {', '.join(SETTINGS_SCHEMA.keys())}",
                ephemeral=True,
            )
            return

        mysql.update_settings(interaction.guild.id, name, channel.id)
        await interaction.response.send_message(
            f"Updated `{name}` to channel `{channel.name}`.", ephemeral=True
        )
    @set_setting.error
    async def set_setting_error(self, interaction: Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "You don't have permission to run this command.",
                ephemeral=True
            )
            raise error

    @app_commands.command(name="help", description="Get help on settings.")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def help(self, interaction: Interaction):
        """Provide help information for settings, showing description, name and expected type."""
        help_message = "Available settings, :\n"
        for setting in SETTINGS_SCHEMA.values():
            help_message += (
                f"**{setting.name}**: {setting.description} (Type: {setting.type.__name__})\n"
            )
        help_message += "\nAvailable commands:\n"
        help_message += "`/set_setting <name> <value>`: Set a server setting.\n"
        help_message += "`/remove_setting <name>`: Remove a server setting.\n"
        help_message += "`/set_channel <name> <channel>`: Set a channel for a setting.\n"
        help_message += "`/get_setting <name>`: Get the current value of a server setting.\n"
        help_message += "`/help`: Get help on settings.\n"
        await interaction.response.send_message(help_message, ephemeral=True)
    @help.error
    async def help_error(self, interaction: Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "You don't have permission to run this command.",
                ephemeral=True
            )
            raise error

    @app_commands.command(name="get_setting", description="Get the current value of a server setting.")
    @app_commands.choices(name=non_channel_choices+channel_choices)
    @app_commands.checks.has_permissions(moderate_members=True)
    async def get_setting(self, interaction: Interaction, name: str):
        """Get the current value of a server setting."""
        schema = SETTINGS_SCHEMA.get(name)
        if not schema:
            await interaction.response.send_message(
                f"Invalid setting name. Available settings: {', '.join(SETTINGS_SCHEMA.keys())}",
                ephemeral=True,
            )
            return

        # Retrieve the current value from the database
        current_value = mysql.get_settings(interaction.guild.id, name)
        if current_value is None:
            await interaction.response.send_message(
                f"`{name}` is not set.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"`{name}` is currently set to `{current_value}`.", ephemeral=True
            )
    @get_setting.error
    async def get_setting_error(self, interaction: Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "You don't have permission to run this command.",
                ephemeral=True
            )
            raise error

async def setup(bot: commands.Bot):
    await bot.add_cog(Settings(bot))