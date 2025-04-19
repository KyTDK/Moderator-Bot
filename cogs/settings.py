from discord.ext import commands
from discord import app_commands, Interaction
import discord
from modules.utils import mysql, time
from modules.config.settings_schema import SETTINGS_SCHEMA
from modules.variables import TimeString

class Settings(commands.Cog):
    """A cog for settings commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # List to hold choices for non-channel settings
    non_channel_choices_without_hidden = [
        app_commands.Choice(name=setting.name, value=setting_name)
        for setting_name, setting in SETTINGS_SCHEMA.items()
        if setting.type != discord.TextChannel and setting.type != list[discord.TextChannel] and setting.hidden is False
    ]
    non_channel_choices_all = [
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

    settings_group = app_commands.Group(
        name="settings",
        description="Manage server settings.",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @settings_group.command(name="help", description="Get help on settings.")
    async def help_settings(self, interaction: Interaction):
        help_message = "**Available Settings:**\n"
        for setting in SETTINGS_SCHEMA.values():
            help_message += (
                f"**{setting.name}**: {setting.description} (Type: {setting.type.__name__})\n"
            )
        await interaction.response.send_message(help_message, ephemeral=True)
    
    @settings_group.command(name="remove", description="Remove a server setting.")
    @app_commands.choices(name=non_channel_choices_all)
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
        if mysql.update_settings(interaction.guild.id, name, None):
            await interaction.response.send_message(
                f"Removed `{name}` setting.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"`{name}` has already been removed.", ephemeral=True
            )

    @settings_group.command(name="reset", description="Wipe all settings are start with default. This can't be undone")
    async def reset(self, interaction: Interaction):
        """Reset server settings."""
        await interaction.response.defer(ephemeral=True)
        _, rows = mysql.execute_query("DELETE FROM settings WHERE guild_id = %s", (interaction.guild.id,))
        if rows>0:
            await interaction.followup.send("Reset all settings to defaults.")
        else:
            await interaction.followup.send("You are already using default settings.")

    @settings_group.command(name="set", description="Set a server setting.")
    @app_commands.choices(name=non_channel_choices_without_hidden)
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
                raise ValueError(
                    f"**`{name}` expects a channel.**\n"
                    f"Use `/set_channel {name} #channel-name` instead."
                )
            elif expected is TimeString:
                try:
                    parsed = TimeString(value)
                except ValueError:
                    raise ValueError("Invalid duration format. Use formats like 20s, 30m, 2h, 30d, 2w, 5mo, 1y. Seconds, minutes, hours, days, weeks, months and years respectively.",)
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

    @settings_group.command(name="channel_set", description="Set a channel for a setting.")
    @app_commands.choices(name=channel_choices)
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
    
    @settings_group.command(name="channel_remove", description="Remove a channel from a setting.")
    @app_commands.choices(name=channel_choices)
    async def remove_channel(self, interaction: Interaction, name: str, channel: discord.TextChannel):
        await interaction.response.defer(ephemeral=True)

        schema = SETTINGS_SCHEMA.get(name)
        if not schema or schema.type not in (discord.TextChannel, list[discord.TextChannel]):
            await interaction.followup.send("Invalid setting name or type.", ephemeral=True)
            return

        current = mysql.get_settings(interaction.guild.id, name)

        if schema.type == list[discord.TextChannel]:
            channels = current or []
            if channel.id not in channels:
                await interaction.followup.send(f"`{channel.name}` is not in `{name}`.", ephemeral=True)
                return

            channels.remove(channel.id)
            mysql.update_settings(interaction.guild.id, name, channels)
            await interaction.followup.send(f"Removed `{channel.name}` from `{name}`.", ephemeral=True)
        else:
            if current != channel.id:
                await interaction.followup.send(f"`{channel.name}` is not set for `{name}`.", ephemeral=True)
                return

            mysql.update_settings(interaction.guild.id, name, None)
            await interaction.followup.send(f"Removed `{channel.name}` from `{name}`.", ephemeral=True)
 
    @app_commands.command(name="help", description="Get help.")
    @app_commands.default_permissions(moderate_members=True)
    async def help(self, interaction: Interaction):
        """Provide help information for settings, showing description, name, and expected type."""
        help_message = "**Available Settings:**\n"
        help_message += ("Use `/settings help` for help on available settings\n")

        help_message += "\n**Available Commands:**\n"
        for command in self.bot.tree.walk_commands():
            # Skip commands the user doesn't have permission to use
            if command.default_permissions:
                # Check if the user has the required permissions
                permissions = command.default_permissions
                if not interaction.user.guild_permissions.is_superset(permissions):
                    continue
            # Format command name with its parent group if it exists
            full_command_name = f"/{command.qualified_name}"
            description = command.description or "No description provided."
            help_message += f"`{full_command_name}`: {description}\n"

        # Support and donation links
        help_message += (
            "**Support Server:** <https://discord.gg/invite/33VcwjfEXC>\n"
            "**Donation link:** <https://www.paypal.com/donate/?hosted_button_id=9FAG4EDFBBRGC>"
        )

        await interaction.response.send_message(help_message, ephemeral=True)

    @settings_group.command(name="get", description="Get the current value of a server setting.")
    @app_commands.choices(name=non_channel_choices_all+channel_choices)
    async def get_setting(self, interaction: Interaction, name: str):
        """Get the current value of a server setting."""
        await interaction.response.defer(ephemeral=True)
        schema = SETTINGS_SCHEMA.get(name)
        if not schema:
            await interaction.followup.send(
                f"Invalid setting name.",
                ephemeral=True,
            )
            return

        # Retrieve the current value from the database
        current_value = mysql.get_settings(interaction.guild.id, name)
        type = schema.type
        if current_value is None:
            await interaction.followup.send(
                f"`{name}` is not set.", ephemeral=True
            )
        else:
            if schema.private:
                await interaction.followup.send(
                    "For privacy reasons, this setting is hidden."
                )
                return
            # If the setting is a list of channels, convert to channel mentions
            if type == list[discord.TextChannel]:
                # Convert channel IDs to mentions
                channel_mentions = [f"<#{channel_id}>" for channel_id in current_value]
                await interaction.followup.send(
                    f"`{name}` is currently set to {', '.join(channel_mentions)}.", ephemeral=True
                )
            elif type == discord.TextChannel:
                # Convert channel ID to mention
                channel_mention = f"<#{current_value}>"
                await interaction.followup.send(
                    f"`{name}` is currently set to {channel_mention}.", ephemeral=True
                )
            elif type == dict[int, tuple[str, str]]:
                # Convert the dictionary to a string representation
                strike_actions = ", ".join(
                    [f"{k}: {v[0]} {v[1] or ''}" for k, v in current_value.items()]
                )
                await interaction.followup.send(
                    f"`{name}` is currently set to `{strike_actions}`.", ephemeral=True
                )
            else:
                await interaction.followup.send(
                    f"`{name}` is currently set to `{current_value}`.", ephemeral=True
                )

    # Command to configure strike actions, allowing user to define punishment for x amount of strikes, so it will be a list of actions they can define
    @settings_group.command(name="strike", description="Configure strike actions.")
    @app_commands.describe(
        number_of_strikes="Number of strikes required to trigger the action.",
        action="Action to take (ban, kick, timeout).",
        duration="Duration for mute action (e.g., 1h, 30m, 30d). Leave empty for permanent or if not applicable.",
    )
    @app_commands.choices(
        action=[
            app_commands.Choice(name="Permanent ban", value="ban"),
            app_commands.Choice(name="Kick", value="kick"),
            app_commands.Choice(name="Timeout", value="timeout"),
            app_commands.Choice(name="Remove action", value="remove"),
        ]
    )
    async def strike_action(self, interaction: Interaction, number_of_strikes: int, action: str, duration: str = None):
        """Configure strike actions."""
        await interaction.response.defer(ephemeral=True)
        # Validate action and duration
        valid_actions = ["ban", "kick", "timeout", "remove"]
        if action not in valid_actions:
            await interaction.followup.send(
                f"Invalid action. Valid actions are: {', '.join(valid_actions)}.",
                ephemeral=True,
            )
            return

        if action == "timeout" and duration is None:
            await interaction.followup.send(
                "Duration is required for timeout action.",
                ephemeral=True,
            )
            return
        
        if time.parse_duration(duration) is None and duration is not None:
            await interaction.followup.send(
                "Invalid duration format. Use formats like 20s, 30m, 2h, 30d, 2w, 5mo, 1y. Seconds, minutes, hours, days, weeks, months and years respectively.",
                ephemeral=True,
            )
            return

        strike_actions = mysql.get_settings(interaction.guild.id, "strike-actions") or {}

        number_of_strikes = str(number_of_strikes)

        if action == "remove":
            # Remove the action
            if number_of_strikes in strike_actions:
                removed_action = strike_actions.pop(number_of_strikes)
                mysql.update_settings(interaction.guild.id, "strike-actions", strike_actions)
                await interaction.followup.send(
                    f"Removed strike action for `{number_of_strikes}` strikes: `{removed_action}`.",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    f"No strike action found for `{number_of_strikes}` strikes.",
                    ephemeral=True,
                )
            return

        # Update the dictionary, telling the user the new and old values
        if number_of_strikes in strike_actions:
            old_action = strike_actions[number_of_strikes]
            strike_actions[number_of_strikes] = (action, duration)
            await interaction.followup.send(
                f"Updated strike action for `{number_of_strikes}` strikes: `{old_action}` -> `{action}` {duration or ''}.",
                ephemeral=True,
            )
        else:
            strike_actions[number_of_strikes] = (action, duration)
            await interaction.followup.send(
                f"Added strike action for `{number_of_strikes}` strikes: `{action}` {duration or ''}.",
                ephemeral=True,
            )

        # Store the strike action in the database
        mysql.update_settings(interaction.guild.id, "strike-actions", strike_actions)

async def setup(bot: commands.Bot):
    await bot.add_cog(Settings(bot))