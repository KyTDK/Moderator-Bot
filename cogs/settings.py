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
            app_commands.Choice(name=choice[:100], value=choice[:100])
            for choice in schema.choices if current.lower() in choice.lower()
        ][:25]

    # List to hold choices for non-channel settings
    choices_without_hidden = [
        app_commands.Choice(name=setting.name[:100], value=setting_name[:100])
        for setting_name, setting in SETTINGS_SCHEMA.items()
        if setting.hidden is False
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
            if getattr(setting, "hidden", False):
                continue
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
    @app_commands.choices(name=choices_without_hidden)
    async def set_setting(
        self,
        interaction: Interaction,
        name: str,
        value: str = None,
        channel: Optional[discord.TextChannel] = None,
        role: Optional[discord.Role] = None
    ):
        await interaction.response.defer(ephemeral=True)
        schema = SETTINGS_SCHEMA.get(name)
        if not schema:
            await interaction.followup.send("**Invalid setting name.**", ephemeral=True)
            return

        expected = schema.type
        try:
            # Validate required parameters for expected type
            if expected == bool and value is None:
                raise ValueError(f"**`{name}` expects a boolean. Use the `value` option.**")
            if expected == int and value is None:
                raise ValueError(f"**`{name}` expects an integer. Use the `value` option.**")
            if expected == TimeString and value is None:
                raise ValueError(f"**`{name}` expects a duration (e.g. 30m, 1d).**")
            if expected == discord.TextChannel and channel is None:
                raise ValueError(f"**`{name}` expects a channel. Use the `channel` option.**")
            if expected == discord.Role and role is None:
                raise ValueError(f"**`{name}` expects a role. Use the `role` option.**")
            if expected == list[discord.TextChannel] and channel is None:
                raise ValueError(f"**`{name}` expects a channel to add. Use the `channel` option.**")
            if expected == list[discord.Role] and role is None:
                raise ValueError(f"**`{name}` expects a role to add. Use the `role` option.**")

            # Type conversion
            if expected == int:
                parsed = int(value)
            elif expected == bool:
                low = str(value).lower()
                if low in ("true", "1", "yes"):
                    parsed = True
                elif low in ("false", "0", "no"):
                    parsed = False
                else:
                    raise ValueError(f"**`{name}` expects a boolean.**")
            elif expected == TimeString:
                parsed = TimeString(value)
            elif expected == discord.TextChannel:
                parsed = channel.id
            elif expected == discord.Role:
                parsed = role.id
            elif expected == list[discord.TextChannel]:
                current = await mysql.get_settings(interaction.guild.id, name) or []
                if channel.id not in current:
                    current.append(channel.id)
                parsed = current
            elif expected == list[discord.Role]:
                current = await mysql.get_settings(interaction.guild.id, name) or []
                if role.id not in current:
                    current.append(role.id)
                parsed = current
            else:
                parsed = value

            await schema.validate(parsed)
            await mysql.update_settings(interaction.guild.id, name, parsed)
            await interaction.followup.send(f"Updated `{name}` to `{parsed}`.", ephemeral=True)

        except ValueError as ve:
            await interaction.followup.send(str(ve), ephemeral=True)

        except Exception as e:
            traceback.print_exc()
            await interaction.followup.send(
                f"An unexpected error occurred: `{e}`", ephemeral=True
            )

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
    @app_commands.choices(
        name=choices_without_hidden[:25]
    )
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
        if not current_value:
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
            elif type == dict[str, list[str]]:
                strike_actions = ", ".join(
                    [f"{k}: {', '.join(v)}" for k, v in current_value.items()]
                )
                await interaction.followup.send(
                    f"`{name}` is currently set to `{strike_actions}`.", ephemeral=True
                )
            else:
                await interaction.followup.send(
                    f"`{name}` is currently set to `{current_value}`.", ephemeral=True
                )

    @settings_group.command(name="remove", description="Remove a server setting or an item from a list-type setting.")
    @app_commands.choices(name=choices_without_hidden[:25])
    async def remove_setting(
        self,
        interaction: Interaction,
        name: str,
        channel: Optional[discord.TextChannel] = None,
        role: Optional[discord.Role] = None
    ):
        await interaction.response.defer(ephemeral=True)
        schema = SETTINGS_SCHEMA.get(name)
        if not schema:
            await interaction.followup.send("Invalid setting name.", ephemeral=True)
            return

        current = await mysql.get_settings(interaction.guild.id, name)
        expected = schema.type

        if not current:
            await interaction.followup.send(f"`{name}` is not set.", ephemeral=True)
            return

        try:
            if expected == list[discord.TextChannel]:
                if not channel:
                    raise ValueError("You must specify a channel to remove.")
                if channel.id not in current:
                    raise ValueError(f"{channel.mention} is not in `{name}`.")
                current.remove(channel.id)
                await mysql.update_settings(interaction.guild.id, name, current)
                await interaction.followup.send(f"Removed {channel.mention} from `{name}`.", ephemeral=True)

            elif expected == list[discord.Role]:
                if not role:
                    raise ValueError("You must specify a role to remove.")
                if role.id not in current:
                    raise ValueError(f"{role.mention} is not in `{name}`.")
                current.remove(role.id)
                await mysql.update_settings(interaction.guild.id, name, current)
                await interaction.followup.send(f"Removed {role.mention} from `{name}`.", ephemeral=True)

            else:
                await mysql.update_settings(interaction.guild.id, name, None)
                await interaction.followup.send(f"Removed setting `{name}`. Now using default.", ephemeral=True)

        except ValueError as ve:
            await interaction.followup.send(str(ve), ephemeral=True)

        except Exception as e:
            traceback.print_exc()
            await interaction.followup.send(f"An unexpected error occurred: `{e}`", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(Settings(bot))