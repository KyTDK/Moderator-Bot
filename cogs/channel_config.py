import discord
from discord.ext import commands
from discord import app_commands, Interaction
from modules.utils import mysql

LOG_CHANNEL_TYPES = {
    "Strike": "strike-channel",
    "NSFW": "nsfw-channel",
    "AI": "aimod-channel",
    "Monitor": "monitor-channel",
    "VC Transcript": "vcmod-transcript-channel",
}

class ChannelConfigCog(commands.Cog):
    def __init__(self, bot): self.bot = bot

    channels_group = app_commands.Group(
        name="channels",
        description="Configure log channels.",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @channels_group.command(name="set", description="Set a log channel.")
    @app_commands.describe(
        channel="The channel to use for logging.",
        type="Which type of log this channel is for."
    )
    @app_commands.choices(
        type=[app_commands.Choice(name=name, value=name) for name in LOG_CHANNEL_TYPES]
    )
    async def set_channel(
        self,
        interaction: Interaction,
        channel: discord.TextChannel,
        type: app_commands.Choice[str],
    ):
        key = LOG_CHANNEL_TYPES[type.value]

        if type.value == "NSFW" and not channel.is_nsfw():
            await interaction.response.send_message(
                f"{channel.mention} is not age-restricted. Please choose an NSFW channel.",
                ephemeral=True,
            )
            return
        
        # Check if bot has required permissions in the channel
        required_perms = [
            "view_channel", 
            "send_messages", 
            "embed_links", 
            "attach_files"
        ]
        perms = channel.permissions_for(interaction.guild.me)

        missing = [p for p in required_perms if not getattr(perms, p)]
        if missing:
            await interaction.response.send_message(
                f"I am missing the following permissions in {channel.mention}: "
                + ", ".join(m.replace('_', ' ').title() for m in missing),
                ephemeral=True,
            )
            return

        await mysql.update_settings(interaction.guild.id, key, channel.id)
        await interaction.response.send_message(
            f"{type.value} logs will be posted in {channel.mention}.",
            ephemeral=True,
        )

    @channels_group.command(name="unset", description="Unset a log channel.")
    @app_commands.describe(type="Which log type to disable.")
    @app_commands.choices(
        type=[app_commands.Choice(name=name, value=name) for name in LOG_CHANNEL_TYPES]
    )
    async def unset_channel(self, interaction: Interaction, type: app_commands.Choice[str]):
        key = LOG_CHANNEL_TYPES[type.value]
        await mysql.update_settings(interaction.guild.id, key, None)
        await interaction.response.send_message(
            f"{type.value} log channel has been unset.",
            ephemeral=True,
        )

    @channels_group.command(name="show", description="Show current log channel settings.")
    async def show_channels(self, interaction: Interaction):
        # Ensure guild ID is an int
        guild_id = interaction.guild.id
        if isinstance(guild_id, str):
            try:
                guild_id = int(guild_id)
            except ValueError:
                await interaction.response.send_message(
                    "Error: Invalid guild ID format.", ephemeral=True
                )
                return

        settings = await mysql.get_settings(
            guild_id,
            list(LOG_CHANNEL_TYPES.values())
        ) or {}

        def fmt(name, key):
            cid = settings.get(key)
            if isinstance(cid, str):
                try:
                    cid = int(cid)
                except ValueError:
                    cid = None
            ch = interaction.guild.get_channel(cid) if cid else None
            return f"{name}: {ch.mention if ch else 'Not set'}"

        lines = [fmt(name, key) for name, key in LOG_CHANNEL_TYPES.items()]
        await interaction.response.send_message(
            "**Log Channels:**\n" + "\n".join(lines),
            ephemeral=True
        )

async def setup(bot: commands.Bot):
    await bot.add_cog(ChannelConfigCog(bot))
