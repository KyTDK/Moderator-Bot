from typing import Optional
import discord
from discord.ext import commands
from modules.utils import mysql
from discord.app_commands.errors import MissingPermissions

class Monitoring(commands.Cog):
    """A cog for monitoring various server events and logging them to a monitor channel."""
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def get_monitor_channel(self, guild_id: int) -> Optional[int]:
        id = await mysql.get_settings(guild_id, "monitor-channel")
        if id:
            id = int(id)
        return id


    async def log_event(self, guild: discord.Guild, message: str):
        """Sends the log message to the monitor channel if set."""
        channel_id = await self.get_monitor_channel(guild.id)
        if channel_id:
            channel = guild.get_channel(channel_id)
            if channel:
                await channel.send(message)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        message = f":green_circle: **Member Joined:** {member.mention} has joined the server."
        await self.log_event(member.guild, message)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        message = f":red_circle: **Member Left:** {member.mention} has left the server."
        await self.log_event(member.guild, message)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        channel = self.bot.get_channel(payload.channel_id)
        if channel is None:
            return  # Channel not found

        # Attempt to retrieve the message from cache
        message = payload.cached_message
        if message:
            if message.author.bot:
                return  # Ignore bot messages
            log_message = (f":wastebasket: **Message Deleted:** In {channel.mention}, "
                        f"{message.author.mention} said: {message.content}")
        else:
            log_message = f":wastebasket: A message was deleted in {channel.mention}, but content is unavailable."

        await self.log_event(channel.guild, log_message)

    @commands.Cog.listener()
    async def on_reaction_add(self, reaction: discord.Reaction, user: discord.User):
        # Ignore reactions from bots
        if user.bot:
            return
        log_message = (f":thumbsup: **Reaction Added:** {user.mention} added {reaction.emoji} "
                       f"to a message in #{reaction.message.channel.mention}.")
        await self.log_event(reaction.message.guild, log_message)

    @commands.Cog.listener()
    async def on_command_error(self, ctx: commands.Context, error: Exception):
        # Log permission errors specifically
        if isinstance(error, MissingPermissions):
            log_message = (f":no_entry: **Permission Error:** {ctx.author.name} attempted to run "
                           f"'{ctx.command}' without the required permissions.")
            await self.log_event(ctx.guild, log_message)
        else:
            # Optionally, log other command errors
            log_message = (f":warning: **Command Error:** An error occurred in command '{ctx.command}': {error}")
            await self.log_event(ctx.guild, log_message)
            
async def setup(bot: commands.Bot):
    await bot.add_cog(Monitoring(bot))
