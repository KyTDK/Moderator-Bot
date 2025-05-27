from typing import Optional
from discord import Embed, Color
import discord
from discord.ext import commands
from modules.utils import mysql
from discord.app_commands.errors import MissingPermissions
from discord.utils import format_dt, utcnow

class Monitoring(commands.Cog):
    """A cog for monitoring various server events and logging them to a monitor channel."""
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.invite_cache = {}

    async def get_monitor_channel(self, guild_id: int) -> Optional[int]:
        id = await mysql.get_settings(guild_id, "monitor-channel")
        if id:
            id = int(id)
        return id


    async def log_event(self, guild: discord.Guild, message=None, embed=None):
        channel_id = await self.get_monitor_channel(guild.id)
        if channel_id:
            channel = guild.get_channel(channel_id)
            if channel:
                try:
                    await channel.send(content=message if message else None, embed=embed)
                except discord.Forbidden:
                    print(f"Missing access to send messages in channel ID {channel.id}")

    @commands.Cog.listener()
    async def on_ready(self):
        for guild in self.bot.guilds:
            try:
                self.invite_cache[guild.id] = await guild.invites()
            except Exception as e:
                print(f"Failed to fetch invites for {guild.name}: {e}")

    @commands.Cog.listener()
    async def on_invite_create(self, invite):
        try:
            self.invite_cache[invite.guild.id] = await invite.guild.invites()
        except Exception as e:
            print(f"Failed to update invites for {invite.guild.name}: {e}")

    @commands.Cog.listener()
    async def on_invite_delete(self, invite):
        try:
            self.invite_cache[invite.guild.id] = await invite.guild.invites()
        except Exception as e:
            print(f"Failed to update invites for {invite.guild.name}: {e}")

    @commands.Cog.listener()
    async def on_member_join(self, member):
        try:
            # Fetch the updated list of invites
            new_invites = await member.guild.invites()
            old_invites = self.invite_cache.get(member.guild.id, [])

            # Find the invite that was used
            used_invite = None
            for old_invite in old_invites:
                for new_invite in new_invites:
                    if old_invite.code == new_invite.code and old_invite.uses < new_invite.uses:
                        used_invite = new_invite
                        break
                if used_invite:
                    break

            # Update the cache
            self.invite_cache[member.guild.id] = new_invites

            # Create the embed message
            embed = Embed(
                title="Member Joined",
                description=f"{member.mention} has joined the server.",
                color=Color.green()
            )

            avatar_url = member.avatar.url if member.avatar else member.default_avatar.url
            embed.set_thumbnail(url=avatar_url)

            embed.add_field(
                name="Account Created",
                value=format_dt(member.created_at or utcnow(), style='F'),
                inline=True
            )

            embed.add_field(
                name="Joined At",
                value=format_dt(member.joined_at or utcnow(), style='F'),
                inline=True
            )

            if used_invite:
                inviter = used_invite.inviter
                embed.add_field(
                    name="Invited By",
                    value=f"{inviter.mention} (Code: {used_invite.code})",
                    inline=True
                )
            else:
                embed.add_field(
                    name="Invited By",
                    value="Could not determine inviter.",
                    inline=True
                )

            embed.set_footer(text="Bot account" if member.bot else "User account")

            await self.log_event(member.guild, embed=embed)

        except Exception as e:
            print(f"Failed to log member join: {e}")

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        try:
            embed = Embed(
                title="Member Left",
                description=f"{member.name} has left the server.",
                color=Color.red()
            )

            avatar_url = member.avatar.url if member.avatar else member.default_avatar.url
            embed.set_thumbnail(url=avatar_url)

            if member.joined_at:
                duration = utcnow() - member.joined_at
                days = duration.days
                embed.add_field(
                    name="Time in Server",
                    value=f"{days} day(s)",
                    inline=True
                )

            embed.set_footer(text="Bot account" if member.bot else "User account")

            await self.log_event(member.guild, embed=embed)

        except Exception as e:
            print(f"Failed to log member leave: {e}")

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

        try:
            # Safely get channel name or fallback
            channel = reaction.message.channel
            if isinstance(channel, discord.TextChannel):
                channel_name = f"#{channel.name}"
            else:
                channel_name = f"Channel ID {channel.id}"

            # Safely get guild
            guild = reaction.message.guild
            if not guild:
                return  # Skip if we can't resolve the guild

            log_message = (f":thumbsup: **Reaction Added:** {user.mention} added {reaction.emoji} "
                        f"to a message in {channel_name}.")

            await self.log_event(guild, log_message)

        except AttributeError as e:
            print(f"[on_reaction_add] Attribute error: {e}")
        except discord.Forbidden:
            print(f"[on_reaction_add] Missing access when trying to log event.")
        except Exception as e:
            print(f"[on_reaction_add] Unexpected error: {e}")
    
    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        # Only log when content actually changes and ignore bot messages
        if before.author.bot or before.content == after.content:
            return
        log_message = (f":pencil2: **Message Edited:** In {before.channel.mention}, "
                       f"{before.author.mention} changed from:\n`{before.content}`\n to:\n`{after.content}`")
        await self.log_event(before.guild, log_message)

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
