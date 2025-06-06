from typing import Optional
from discord.ext import commands
from discord import app_commands, Interaction
import discord
from modules.utils import mysql
from modules.config.settings_schema import SETTINGS_SCHEMA
import traceback
from modules.variables.TimeString import TimeString

MAX_CHARS = 1900  # Leave buffer for formatting
CHUNK_SEPARATOR = "\n"

def paginate(text, limit=MAX_CHARS):
    chunks = []
    lines = text.split(CHUNK_SEPARATOR)
    current = ""

    for line in lines:
        if len(current) + len(line) + len(CHUNK_SEPARATOR) > limit:
            chunks.append(current)
            current = ""
        current += line + CHUNK_SEPARATOR
    if current:
        chunks.append(current)
    return chunks

class Settings(commands.Cog):
    """A cog for settings commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def value_autocomplete(this, interaction: Interaction, current: str) -> list[app_commands.Choice[str]]:
        name_option = getattr(interaction.namespace, "name", None)
        if not name_option:
            return []
        schema = SETTINGS_SCHEMA.get(name_option)
        if not schema or not schema.choices:
            return []
        return [
            app_commands.Choice(name=choice, value=choice)
            for choice in schema.choices if current.lower() in choice.lower()
        ][:25]

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

        chunks = paginate(help_message)
        await interaction.response.send_message(chunks[0], ephemeral=True)
        for chunk in chunks[1:]:
            await interaction.followup.send(chunk, ephemeral=True)

    @settings_group.command(name="reset", description="Wipe all settings and start with defaults. This can't be undone")
    async def reset(self, interaction: Interaction):
        """Reset server settings."""
        await interaction.response.defer(ephemeral=True)
        _, rows = await mysql.execute_query("DELETE FROM settings WHERE guild_id = %s", (interaction.guild.id,))
        if rows>0:
            await interaction.followup.send("Reset all settings to defaults.")
        else:
            await interaction.followup.send("You are already using default settings.")

    @settings_group.command(name="set", description="Set a server setting.")
    @app_commands.autocomplete(value=value_autocomplete)
    @app_commands.choices(name=non_channel_choices_without_hidden)
    async def set_setting(self, interaction: Interaction, name: str, value: str):
        await interaction.response.defer(ephemeral=True)
        schema = SETTINGS_SCHEMA.get(name)
        if not schema:
            await interaction.response.send_message(
                f"**Invalid setting name.",
                ephemeral=True,
            )
            return

        expected = schema.type
        try:
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
                parsed = TimeString(value)
            else:
                # fallback to string
                parsed = value

            try:
                await schema.validate(parsed)
            except Exception as e:
                await interaction.followup.send(content=str(e), ephemeral=True)
                return

            await mysql.update_settings(interaction.guild.id, name, parsed)
            await interaction.followup.send(
                f"Updated `{name}` to `{parsed}`.", ephemeral=True
            )

        except ValueError as ve:
            await interaction.followup.send(str(ve), ephemeral=True)

        except Exception as e:
            traceback.print_exc()
            await interaction.followup.send(
                f"An unexpected error occurred: `{e}`",
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
            current_channels = await mysql.get_settings(interaction.guild.id, name) or []
            if channel.id not in current_channels:
                current_channels.append(channel.id)
                await mysql.update_settings(interaction.guild.id, name, current_channels)
                await interaction.response.send_message(
                    f"Added `{channel.name}` to `{name}`.", ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    f"`{channel.name}` is already in `{name}`.", ephemeral=True
                )
            return
        await mysql.update_settings(interaction.guild.id, name, channel.id)
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

        current = await mysql.get_settings(interaction.guild.id, name)

        if schema.type == list[discord.TextChannel]:
            channels = current or []
            if channel.id not in channels:
                await interaction.followup.send(f"`{channel.name}` is not in `{name}`.", ephemeral=True)
                return

            channels.remove(channel.id)
            await mysql.update_settings(interaction.guild.id, name, channels)
            await interaction.followup.send(f"Removed `{channel.name}` from `{name}`.", ephemeral=True)
        else:
            if current != channel.id:
                await interaction.followup.send(f"`{channel.name}` is not set for `{name}`.", ephemeral=True)
                return

            await mysql.update_settings(interaction.guild.id, name, None)
            await interaction.followup.send(f"Removed `{channel.name}` from `{name}`.", ephemeral=True)
 
    @app_commands.command(name="help", description="Get help on a specific command group.")
    @app_commands.describe(command="Optional: command group to get help with")
    @app_commands.default_permissions(moderate_members=True)
    async def help(self, interaction: Interaction, command: Optional[str] = None):
        await interaction.response.defer(ephemeral=True)

        if command:
            group = next((cmd for cmd in self.bot.tree.walk_commands() if cmd.name == command and isinstance(cmd, app_commands.Group)), None)
            if not group:
                await interaction.followup.send(f"No help found for `{command}`.", ephemeral=True)
                return

            help_message = f"**/{group.name}** - {group.description or 'No description'}\n\n"
            for sub in group.commands:
                help_message += f"`/{sub.qualified_name}`: {sub.description or 'No description'}\n"

        else:
            help_message = "**Available Command Groups:**\n\n"
            top_level = [cmd for cmd in self.bot.tree.walk_commands() if isinstance(cmd, app_commands.Group)]
            for group in top_level:
                help_message += f"`/{group.name}`: {group.description or 'No description'} — Try `/help {group.name}` for subcommands\n"

            help_message += (
                "\n**Support Server:** <https://discord.gg/invite/33VcwjfEXC>\n"
                "**Donate:** <https://www.paypal.com/donate/?hosted_button_id=9FAG4EDFBBRGC>"
            )

        chunks = paginate(help_message)
        await interaction.followup.send(chunks[0], ephemeral=True)
        for chunk in chunks[1:]:
            await interaction.followup.send(chunk, ephemeral=True)

    @help.autocomplete("command")
    async def help_autocomplete(self, interaction: Interaction, current: str):
        return [
            app_commands.Choice(name=cmd.name, value=cmd.name)
            for cmd in self.bot.tree.walk_commands()
            if isinstance(cmd, app_commands.Group) and current.lower() in cmd.name.lower()
        ][:25]

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
        current_value = await mysql.get_settings(interaction.guild.id, name)
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

async def setup(bot: commands.Bot):
    await bot.add_cog(Settings(bot))