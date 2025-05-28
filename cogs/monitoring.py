from typing import Optional
from discord import Embed, Color
import discord
from discord.ext import commands
from modules.utils import mysql
from discord.app_commands.errors import MissingPermissions
from discord.utils import format_dt, utcnow

class Monitoring(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.invite_cache = {}

    async def get_monitor_channel(self, guild_id: int) -> Optional[int]:
        id = await mysql.get_settings(guild_id, "monitor-channel")
        return int(id) if id else None

    async def log_event(self, guild: discord.Guild, message=None, embed=None, mention_user: bool = True):
        channel_id = await self.get_monitor_channel(guild.id)
        if channel_id:
            channel = guild.get_channel(channel_id)
            if channel:
                try:
                    allowed = discord.AllowedMentions.all() if mention_user else discord.AllowedMentions.none()
                    await channel.send(content=message if message else None, embed=embed, allowed_mentions=allowed)
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
        self.invite_cache[invite.guild.id] = await invite.guild.invites()

    @commands.Cog.listener()
    async def on_invite_delete(self, invite):
        self.invite_cache[invite.guild.id] = await invite.guild.invites()

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

    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        channel = self.bot.get_channel(payload.channel_id)
        if not channel:
            return

        message = payload.cached_message
        if message and not message.author.bot:
            user = message.author
            embed = Embed(
                title="Message Deleted",
                description=(
                    f"**Author:** {user.mention} ({user.name})\n"
                    f"**Channel:** {channel.mention}\n"
                    f"**Content:**\n{message.content or '[No Text Content]'}"
                ),
                color=Color.orange()
            )
            embed.set_footer(text=f"User ID: {user.id}")

            if message.attachments:
                bullet_links: list[str] = [
                    f"• [{att.filename}]({att.url})"
                    for att in message.attachments
                ]
                embed.add_field(
                    name=f"Attachments ({len(bullet_links)})",
                    value="\n".join(bullet_links)[:1024],
                    inline=False
                )
        else:
            embed = Embed(
                title="Message Deleted",
                description=f"A message was deleted in {channel.mention}, but content is unavailable.",
                color=Color.orange()
            )

        await self.log_event(channel.guild, embed=embed, mention_user=False)

    @commands.Cog.listener()
    async def on_reaction_add(self, reaction: discord.Reaction, user: discord.User):
        if user.bot:
            return
        try:
            channel = reaction.message.channel
            guild = reaction.message.guild
            if not guild:
                return

            embed = Embed(
                title="Reaction Added",
                description=(f"{user.mention} ({user.name}) reacted with {reaction.emoji} in {channel.mention}"),
                color=Color.blue()
            )
            embed.set_footer(text=f"User ID: {user.id}")
            await self.log_event(guild, embed=embed, mention_user=False)

        except Exception as e:
            print(f"[on_reaction_add] Error: {e}")

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if before.author.bot or before.content == after.content:
            return

        embed = Embed(
            title="Message Edited",
            description=f"**Author:** {before.author.mention} ({before.author.name})\n"
                        f"**Channel:** {before.channel.mention}\n",
            color=Color.gold()
        )
        embed.add_field(name="Before", value=before.content or "[No Content]", inline=False)
        embed.add_field(name="After", value=after.content or "[No Content]", inline=False)
        embed.set_footer(text=f"User ID: {before.author.id}")
        await self.log_event(before.guild, embed=embed, mention_user=False)

    @commands.Cog.listener()
    async def on_command_error(self, ctx: commands.Context, error: Exception):
        if isinstance(error, MissingPermissions):
            user = ctx.author
            embed = Embed(
                title="Permission Error",
                description=f"{user.mention} ({user.name}) attempted to run `{ctx.command}` without required permissions.",
                color=Color.red()
            )
        else:
            embed = Embed(
                title="Command Error",
                description=f"An error occurred in `{ctx.command}`:\n```{error}```",
                color=Color.red()
            )
        await self.log_event(ctx.guild, embed=embed, mention_user=False)
            
async def setup(bot: commands.Bot):
    await bot.add_cog(Monitoring(bot))
