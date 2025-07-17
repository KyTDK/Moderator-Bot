import discord
from discord.ext import commands
from discord import app_commands, Interaction

from modules.utils import mysql

class ChannelConfigCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    channels_group = app_commands.Group(
        name="channels",
        description="Configure log channels.",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @channels_group.command(name="strike", description="Set the channel for strike logs.")
    async def set_strike(self, interaction: Interaction, channel: discord.TextChannel):
        await mysql.update_settings(interaction.guild.id, "strike-channel", channel.id)
        await interaction.response.send_message(
            f"Strike logs will be posted in {channel.mention}.", ephemeral=True
        )

    @channels_group.command(name="nsfw", description="Set the channel for NSFW logs.")
    async def set_nsfw(self, interaction: Interaction, channel: discord.TextChannel):
        if not channel.is_nsfw():
            await interaction.response.send_message(
                f"{channel.mention} is not age-restricted. Please choose a channel marked as NSFW.", ephemeral=True
            )
            return

        await mysql.update_settings(interaction.guild.id, "nsfw-channel", channel.id)
        await interaction.response.send_message(
            f"NSFW logs will be posted in {channel.mention}.", ephemeral=True
        )

    @channels_group.command(name="ai", description="Set the channel for AI violation logs.")
    async def set_ai(self, interaction: Interaction, channel: discord.TextChannel):
        await mysql.update_settings(interaction.guild.id, "aimod-channel", channel.id)
        await interaction.response.send_message(
            f"AI violation logs will be posted in {channel.mention}.", ephemeral=True
        )

    @channels_group.command(name="monitor", description="Set the channel for monitoring logs.")
    async def set_monitor(self, interaction: Interaction, channel: discord.TextChannel):
        await mysql.update_settings(interaction.guild.id, "monitor-channel", channel.id)
        await interaction.response.send_message(
            f"Monitoring logs will be posted in {channel.mention}.", ephemeral=True
        )

    @channels_group.command(name="show", description="Show current log channel settings.")
    async def show_channels(self, interaction: Interaction):
        gid = interaction.guild.id
        settings = await mysql.get_settings(
            gid,
            ["strike-channel", "nsfw-channel", "monitor-channel", "aimod-channel"],
        ) or {}

        def mention(cid: int | None) -> str:
            if not cid:
                return "Not set"
            ch = interaction.guild.get_channel(cid)
            return ch.mention if ch else f"`#{cid}`"

        message = (
            "**Log Channels:**\n"
            f"Strike: {mention(settings.get('strike-channel'))}\n"
            f"NSFW: {mention(settings.get('nsfw-channel'))}\n"
            f"AI Violations: {mention(settings.get('aimod-channel'))}\n"
            f"Monitor: {mention(settings.get('monitor-channel'))}"
        )
        await interaction.response.send_message(message, ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(ChannelConfigCog(bot))