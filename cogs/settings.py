from discord.ext import commands
from discord import app_commands
from discord import app_commands, Interaction, Embed, Color
from modules.utils import mysql
from discord.app_commands.errors import MissingPermissions
import json
import io
import discord

class settings(commands.Cog):
    """A cog for settings commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
    
    #get moderation settings
    @app_commands.command(
        name="get_moderation_settings",
        description="Get moderation settings."
    )
    @app_commands.checks.has_permissions(moderate_members=True)
    async def get_moderation_settings(self, interaction: Interaction):
        """Get moderation settings."""
        settings_json = mysql.get_settings(interaction.guild.id)
        settings_list = json.loads(settings_json)

        embed_color = Color.blue()
        embeds = []
        current_embed = Embed(
            title="Moderation Settings",
            description="Current moderation settings.",
            color=embed_color
        )
        total_chars = 0
        # settings: {"nsfw_channel": 1342326581258878986, "strike_channel": 1342685831571443713}
        for label, value in settings_list.items():
            label = str(label)
            value = str(value)
            # If the current embed is full or adding this field would exceed the Discord character limit, start a new embed.
            if len(current_embed.fields) >= 25 or total_chars + len(label) + len(value) > 6000:
                embeds.append(current_embed)
                current_embed = Embed(title="Settings (continued)", color=embed_color)
                total_chars = 0

            current_embed.add_field(name=label, value=value, inline=False)
            total_chars += len(label) + len(value)

        # Always include the final embed
        embeds.append(current_embed)

        # If the settings span multiple embeds, send a text file instead
        if len(embeds) > 1:
            # Build the file content using a list comprehension for clarity
            file_content = "\n\n".join(
                f"{item.get('label', 'No label provided')}\n{item.get('value', 'No value provided')}"
                for item in settings_list
            )
            file_buffer = io.StringIO(file_content)
            file = discord.File(file_buffer, filename="settings.txt")
            await interaction.response.send_message(
                content="The settings are too long to display in a single message. Here is a text file with the settings.",
                file=file,
                ephemeral=True
            )
        else:
            # Otherwise, send the single embed
            await interaction.response.send_message(embed=embeds[0], ephemeral=True)
    @get_moderation_settings.error
    async def get_moderation_settings_error(self, interaction: Interaction, error):
        if isinstance(error, MissingPermissions): 
            await interaction.response.send_message("You don't have permission to run this command", ephemeral=True)
        raise error
    
    #set moderation settings
    @app_commands.command(
        name="set_moderation_settings",
        description="Set moderation settings. Leave key blank to remove the setting."
    )
    @app_commands.checks.has_permissions(moderate_members=True)
    async def set_moderation_settings(self, interaction: Interaction, settings_label: str, setting_key: str):
        """Set moderation settings."""
        mysql.update_settings(interaction.guild.id, settings_label, setting_key)
        await interaction.response.send_message(f"Successfully set the setting {settings_label} to {setting_key}.", ephemeral=True)
    @set_moderation_settings.error
    async def set_moderation_settings_error(self, interaction: Interaction, error):
        if isinstance(error, MissingPermissions):
            await interaction.response.send_message(
                "You don't have permission to run this command.",
                ephemeral=True
            )
        raise error
    
    # set strike channel
    @app_commands.command(
        name="set_strike_channel",
        description="Set the strike channel."
    )
    @app_commands.checks.has_permissions(moderate_members=True)
    async def set_strike_channel(self, interaction: Interaction, channel: discord.TextChannel):
        """Set the strike channel."""
        mysql.update_settings(interaction.guild.id, "strike_channel", channel.id)
        await interaction.response.send_message(f"Successfully set the strike channel to {channel.mention}.", ephemeral=True)
    @set_strike_channel.error
    async def set_strike_channel_error(self, interaction: Interaction, error):
        if isinstance(error, MissingPermissions):
            await interaction.response.send_message(
                "You don't have permission to run this command.",
                ephemeral=True
            )
        raise error
    
    # set nsfw channel
    @app_commands.command(
        name="set_nsfw_channel",
        description="Set the nsfw channel."
    )
    @app_commands.checks.has_permissions(moderate_members=True)
    async def set_nsfw_channel(self, interaction: Interaction, channel: discord.TextChannel):
        """Set the nsfw channel."""
        mysql.update_settings(interaction.guild.id, "nsfw_channel", channel.id)
        await interaction.response.send_message(f"Successfully set the nsfw channel to {channel.mention}.", ephemeral=True)
    @set_nsfw_channel.error
    async def set_nsfw_channel_error(self, interaction: Interaction, error):
        if isinstance(error, MissingPermissions):
            await interaction.response.send_message(
                "You don't have permission to run this command.",
                ephemeral=True
            )
        raise error
    
async def setup(bot: commands.Bot):
    await bot.add_cog(settings(bot))