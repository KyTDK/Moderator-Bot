from discord.ext import commands
from discord import app_commands, Interaction
import discord
from modules.utils import mysql
from modules.config.settings_schema import SETTINGS_SCHEMA
from discord.app_commands import MissingPermissions, AppCommandError

class Settings(commands.Cog):
    """A cog for settings commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        bot.tree.on_error = self.on_app_command_error


    # List to hold choices for non-channel settings
    non_channel_choices = [
        app_commands.Choice(name=setting.name, value=setting_name)
        for setting_name, setting in SETTINGS_SCHEMA.items()
        if setting.type != discord.TextChannel and setting.type != list[discord.TextChannel]
    ]

    # List to hold choices for channel settings
    channel_choices = [
        app_commands.Choice(name=setting.name, value=setting_name)
        for setting_name, setting in SETTINGS_SCHEMA.items()
        if setting.type == discord.TextChannel or setting.type == list[discord.TextChannel]
    ]
    
    @app_commands.command(name="remove_setting", description="Remove a server setting.")
    @app_commands.choices(name=non_channel_choices)
    @app_commands.checks.has_permissions(moderate_members=True)
    async def remove_setting(self, interaction: Interaction, name: str):
        """Remove a server setting."""
        schema = SETTINGS_SCHEMA.get(name)
        if not schema:
            await interaction.response.send_message(
                f"Invalid setting name.",
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
                f"**Invalid setting name.",
                ephemeral=True,
            )
            return

        expected = schema.type
        try:
            # 1) Parse & type‐check
            if expected is int:
                try:
                    parsed = int(value)
                except ValueError:
                    raise ValueError(
                        f"**`{name}` expects an integer.**\n"
                        f"Usage: `/set_setting {name} 42` (whole number, no decimals)"
                    )
            elif expected is bool:
                low = value.lower()
                if low in ("true", "1", "yes"):
                    parsed = True
                elif low in ("false", "0", "no"):
                    parsed = False
                else:
                    raise ValueError(
                        f"**`{name}` expects a boolean.**\n"
                        f"Usage: `/set_setting {name} true` or `/set_setting {name} false`"
                    )
            elif expected is discord.TextChannel:
                # you might handle channel‐mentions elsewhere
                raise ValueError(
                    f"**`{name}` expects a channel.**\n"
                    f"Use `/set_channel {name} #channel-name` instead."
                )
            else:
                # fallback to string
                parsed = value

            # 2) Custom validation
            if not schema.validate(parsed):
                raise ValueError(
                    f"**Invalid value for `{name}`.**\n"
                    f"Please ensure it meets the required criteria."
                )

            # 3) Persist
            mysql.update_settings(interaction.guild.id, name, parsed)
            await interaction.response.send_message(
                f"Updated `{name}` to `{parsed}`.", ephemeral=True
            )

        except ValueError as ve:
            # user‐facing error
            await interaction.response.send_message(str(ve), ephemeral=True)

        except Exception:
            # catch‐all for unexpected issues
            await interaction.response.send_message(
                "An unexpected error occurred while updating your setting. Please try again later.",
                ephemeral=True
            )

    @app_commands.command(name="set_channel", description="Set a channel for a setting.")
    @app_commands.choices(name=channel_choices)
    @app_commands.checks.has_permissions(moderate_members=True)
    async def set_channel(self, interaction: Interaction, name: str, channel: discord.TextChannel):
        schema = SETTINGS_SCHEMA.get(name)
        if not schema or (schema.type != discord.TextChannel and schema.type != list[discord.TextChannel]):
            await interaction.response.send_message(
                f"Invalid setting name or type.",
                ephemeral=True,
            )
            return
        if schema.type == list[discord.TextChannel]:
            # If the setting is a list of channels, append the new channel
            current_channels = mysql.get_settings(interaction.guild.id, name) or []
            if channel.id not in current_channels:
                current_channels.append(channel.id)
                mysql.update_settings(interaction.guild.id, name, current_channels)
                await interaction.response.send_message(
                    f"Added `{channel.name}` to `{name}`.", ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    f"`{channel.name}` is already in `{name}`.", ephemeral=True
                )
            return
        mysql.update_settings(interaction.guild.id, name, channel.id)
        await interaction.response.send_message(
            f"Updated `{name}` to channel `{channel.name}`.", ephemeral=True
        )
    
    @app_commands.command(name="remove_channel", description="Remove a channel from a setting.")
    @app_commands.choices(name=channel_choices)
    @app_commands.checks.has_permissions(moderate_members=True)    
    async def remove_channel(self, interaction: Interaction, name: str, channel: discord.TextChannel):
        schema = SETTINGS_SCHEMA.get(name)
        if not schema or (schema.type != discord.TextChannel and schema.type != list[discord.TextChannel]):
            await interaction.response.send_message(
                f"Invalid setting name or type.",
                ephemeral=True,
            )
            return
        if schema.type == list[discord.TextChannel]:
            # If the setting is a list of channels, remove the channel
            current_channels = mysql.get_settings(interaction.guild.id, name) or []
            if channel.id in current_channels:
                current_channels.remove(channel.id)
                mysql.update_settings(interaction.guild.id, name, current_channels)
                await interaction.response.send_message(
                    f"Removed `{channel.name}` from `{name}`.", ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    f"`{channel.name}` is not in `{name}`.", ephemeral=True
                )
            return

    @app_commands.command(name="help", description="Get help on settings.")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def help(self, interaction: Interaction):
        """Provide help information for settings, showing description, name and expected type."""
        help_message = "Available settings:\n"
        for setting in SETTINGS_SCHEMA.values():
            help_message += (
                f"**{setting.name}**: {setting.description} (Type: {setting.type.__name__})\n"
            )
        help_message += "\nAvailable commands:\n"
        help_message += "`/set_setting <name> <value>`: Set a server setting.\n"
        help_message += "`/remove_setting <name>`: Remove a server setting.\n"
        help_message += "`/remove_channel <name> <channel>`: Remove a channel from a setting.\n"
        help_message += "`/set_channel <name> <channel>`: Set a channel for a setting.\n"
        help_message += "`/get_setting <name>`: Get the current value of a server setting.\n"
        help_message += "`/add_banned_word <word>`: Add a word to the banned words list.\n"
        help_message += "`/remove_banned_word <word>`: Remove a word from the banned words list.\n"
        help_message += "`/list_banned_words`: List all banned words.\n"
        help_message += "`/help`: Get help on settings.\n"
        # support discord servrer link
        help_message += "\nPost suggestions and bugs on the support discord server: [Support Server](https://discord.gg/invite/33VcwjfEXC)"
        await interaction.response.send_message(help_message, ephemeral=True)

    @app_commands.command(name="get_setting", description="Get the current value of a server setting.")
    @app_commands.choices(name=non_channel_choices+channel_choices)
    @app_commands.checks.has_permissions(moderate_members=True)
    async def get_setting(self, interaction: Interaction, name: str):
        """Get the current value of a server setting."""
        schema = SETTINGS_SCHEMA.get(name)
        if not schema:
            await interaction.response.send_message(
                f"Invalid setting name.",
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

    async def on_app_command_error(
    self,
    interaction: Interaction,
    error: AppCommandError):
        # intercept permission errors before Discord’s default
        if isinstance(error, MissingPermissions):
            # send only our custom message
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "You don't have permission to run this command.",
                    ephemeral=True
                )
            else:
                await interaction.followup.send(
                    "You don't have permission to run this command.",
                    ephemeral=True
                )
            return

        # let everything else fall back to the default handler
        await super().on_app_command_error(interaction, error)


async def setup(bot: commands.Bot):
    await bot.add_cog(Settings(bot))