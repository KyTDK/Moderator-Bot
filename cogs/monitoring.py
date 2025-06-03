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
        self.message_cache = {}

    async def get_monitor_channel(self, guild_id: int) -> Optional[int]:
        id = await mysql.get_settings(guild_id, "monitor-channel")
        return int(id) if id else None

    async def log_event(
        self,
        guild: discord.Guild,
        message=None,
        embed=None,
        mention_user: bool = True
    ):
        channel_id = await self.get_monitor_channel(guild.id)
        if channel_id:
            channel = guild.get_channel(channel_id)
            if channel:
                try:
                    allowed = discord.AllowedMentions.all() if mention_user else discord.AllowedMentions.none()
                    await channel.send(
                        content=message if message else None,
                        embed=embed,
                        allowed_mentions=allowed
                    )
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
    async def on_member_join(self, member: discord.Member):
        used_invite: discord.Invite | None = None
        inviter_reason: str | None = None

        new_invites = None
        try:
            new_invites = await member.guild.invites()
        except discord.Forbidden:
            inviter_reason = "Missing Manage Server permission"
            print(f"[Join Log] {inviter_reason} in {member.guild.name}")
        except Exception as e:
            inviter_reason = f"Error while fetching invites ({e})"
            print(f"[Join Log] {inviter_reason}")

        try:
            if new_invites is not None:
                old_invites = self.invite_cache.get(member.guild.id, [])
                for old_invite in old_invites:
                    for new_invite in new_invites:
                        if (
                            old_invite.code == new_invite.code
                            and old_invite.uses < new_invite.uses
                        ):
                            used_invite = new_invite
                            break
                    if used_invite:
                        break
                self.invite_cache[member.guild.id] = new_invites

                if used_invite is None and inviter_reason is None:
                    inviter_reason = "Invite cache empty or invite not yet tracked"
        except Exception as e:
            inviter_reason = f"Error comparing invite usage ({e})"
            print(f"[Join Log] {inviter_reason}")

        try:
            embed = Embed(
                title="Member Joined",
                description=f"{member.mention} ({member.name}) has joined the server.",
                color=Color.green(),
            )

            avatar_url = member.avatar.url if member.avatar else member.default_avatar.url
            embed.set_thumbnail(url=avatar_url)

            embed.add_field(
                name="Account Created",
                value=format_dt(member.created_at or utcnow(), style="F"),
                inline=True,
            )

            embed.add_field(
                name="Joined At",
                value=format_dt(member.joined_at or utcnow(), style="F"),
                inline=True,
            )

            if used_invite:
                inviter = used_invite.inviter
                embed.add_field(
                    name="Invited By",
                    value=f"{inviter.mention} — {inviter.name} • Code: `{used_invite.code}`",
                    inline=True,
                )
            else:
                embed.add_field(
                    name="Invited By",
                    value=f"Could not determine inviter → {inviter_reason or 'Unknown reason'}",
                    inline=True,
                )

            embed.set_footer(text="Bot account" if member.bot else "User account")
            await self.log_event(member.guild, embed=embed)

        except Exception as e:
            print(f"[Join Log] Failed to send join embed for {member}: {e}")

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if before.timed_out_until == after.timed_out_until:
            return

        guild = after.guild
        timed_out = after.timed_out_until is not None
        title = "Member Timed-Out" if timed_out else "Timeout Removed"
        colour = discord.Color.dark_orange() if timed_out else discord.Color.green()

        moderator = reason = None
        try:
            async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.member_update):
                if entry.target.id == after.id and (utcnow() - entry.created_at).total_seconds() < 20:
                    if any(ch.key == "communication_disabled_until" for ch in entry.changes):
                        moderator = entry.user
                        reason = entry.reason
                        break
        except discord.Forbidden:
            pass
        except Exception as e:
            print(f"[Timeout Log] Audit-log lookup failed: {e}")

        embed = Embed(
            title=title,
            description=f"{after.mention} ({after.name}) has "
                        f"{'been placed in' if timed_out else 'had'} a timeout.",
            color=colour
        )

        avatar_url = after.avatar.url if after.avatar else after.default_avatar.url
        embed.set_thumbnail(url=avatar_url)

        if timed_out:
            ts = int(after.timed_out_until.timestamp())
            embed.add_field(name="Ends", value=f"<t:{ts}:F> (<t:{ts}:R>)")

        if moderator:
            embed.add_field(name="Moderator", value=f"{moderator.mention} ({moderator.name})", inline=False)
        if reason:
            embed.add_field(name="Reason", value=reason, inline=False)

        embed.set_footer(text=f"User ID: {after.id}")
        await self.log_event(guild, embed=embed)


    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        try:
            guild = member.guild
            kicked = False
            kicker = None
            reason = None

            try:
                async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.kick):
                    if entry.target.id == member.id and (utcnow() - entry.created_at).total_seconds() < 20:
                        kicked = True
                        kicker = entry.user
                        reason = entry.reason
                        break
            except discord.Forbidden:
                print("[Kick Log] Missing permissions to view audit logs.")
            except Exception as e:
                print(f"[Kick Log] Audit log lookup failed: {e}")

            embed = Embed(
                title="Member Kicked" if kicked else "Member Left",
                description=f"{member.mention} ({member.name}) has {'been kicked' if kicked else 'left'} the server.",
                color=Color.red() if not kicked else Color.orange()
            )

            avatar_url = member.avatar.url if member.avatar else member.default_avatar.url
            embed.set_thumbnail(url=avatar_url)

            if member.joined_at:
                duration = utcnow() - member.joined_at
                days = duration.days
                embed.add_field(name="Time in Server", value=f"{days} day(s)", inline=True)

            if kicked and kicker:
                embed.add_field(name="Kicked By", value=f"{kicker.mention} ({kicker.name})", inline=False)
                if reason:
                    embed.add_field(name="Reason", value=reason, inline=False)

            embed.set_footer(text="Bot account" if member.bot else "User account")
            await self.log_event(guild, embed=embed)

        except Exception as e:
            print(f"[Leave Log] Failed to log member removal: {e}")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.author.bot:
            self.message_cache[message.id] = message

            if len(self.message_cache) > 10000:
                oldest = next(iter(self.message_cache))
                self.message_cache.pop(oldest)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        channel = self.bot.get_channel(payload.channel_id)
        if not channel:
            return

        message = payload.cached_message or self.message_cache.pop(payload.message_id, None)

        user = message.author if message else None

        if user and user.bot:
            return

        deleter: str | None = None
        try:
            async for entry in channel.guild.audit_logs(
                action=discord.AuditLogAction.message_delete,
                limit=5
            ):
                if (
                    entry.extra.channel.id == channel.id and
                    entry.target.id == (user.id if user else None) and
                    (utcnow() - entry.created_at).total_seconds() < 20
                ):
                    deleter = f"{entry.user.mention} ({entry.user.name})"
                    break
        except discord.Forbidden:
            pass
        except Exception as e:
            print(f"[Audit-log lookup] {e}")

        if message:
            embed = Embed(
                title="Message Deleted",
                description=(
                    f"**Author:** {user.mention} ({user.name})\n"
                    f"**Channel:** {channel.mention}\n"
                    + (f"**Deleted by:** {deleter}\n" if deleter else "")
                    + f"**Content:**\n{message.content or '[No Text Content]'}"
                ),
                color=Color.orange()
            )
            embed.set_footer(text=f"User ID: {user.id}")

            if message.attachments:
                links = [f"• [{a.filename}]({a.url})" for a in message.attachments]
                embed.add_field(
                    name=f"Attachments ({len(links)})",
                    value="\n".join(links)[:1024],
                    inline=False
                )

            if message.embeds:
                for i, rich_embed in enumerate(message.embeds):
                    try:
                        parts = []
                        if rich_embed.title:
                            parts.append(f"**{rich_embed.title}**")
                        if rich_embed.description:
                            parts.append(rich_embed.description)
                        if rich_embed.fields:
                            for f in rich_embed.fields:
                                parts.append(f"• **{f.name}**: {f.value}")
                        if rich_embed.url:
                            parts.append(f"[Link]({rich_embed.url})")
                        if rich_embed.author:
                            parts.append(f"_Author: {rich_embed.author.name}_")
                        if rich_embed.footer:
                            parts.append(f"_Footer: {rich_embed.footer.text}_")

                        summary = "\n".join(parts)[:1024]
                        embed.add_field(
                            name=f"Embed {i+1}",
                            value=summary or "*[Embed content unavailable]*",
                            inline=False
                        )
                    except Exception as e:
                        print(f"[Embed parse fail] {e}")
        else:
            embed = Embed(
                title="Message Deleted",
                description=(
                    f"A message was deleted in {channel.mention}."
                    + (f"\n**Deleted by:** {deleter}" if deleter else "")
                ),
                color=Color.orange()
            )

        await self.log_event(channel.guild, embed=embed, mention_user=False)

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User):
        try:
            embed = discord.Embed(
                title="Member Banned",
                description=f"{user.mention} ({user.name}) was banned.",
                color=discord.Color.dark_red()
            )

            avatar_url = user.avatar.url if user.avatar else user.default_avatar.url
            embed.set_thumbnail(url=avatar_url)

            embed.set_footer(text=f"User ID: {user.id}")

            # Attempt to find the moderator responsible via audit logs
            try:
                async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.ban):
                    if entry.target.id == user.id and (discord.utils.utcnow() - entry.created_at).total_seconds() < 20:
                        embed.add_field(name="Banned By", value=f"{entry.user.mention} ({entry.user.name})", inline=False)
                        if entry.reason:
                            embed.add_field(name="Reason", value=entry.reason, inline=False)
                        break
            except discord.Forbidden:
                pass  # Bot lacks permission to view audit logs

            await self.log_event(guild, embed=embed)

        except Exception as e:
            print(f"[Ban Log] Failed to log ban for {user}: {e}")


    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User):
        try:
            embed = discord.Embed(
                title="Member Unbanned",
                description=f"{user.mention} ({user.name}) was unbanned.",
                color=discord.Color.green()
            )

            avatar_url = user.avatar.url if user.avatar else user.default_avatar.url
            embed.set_thumbnail(url=avatar_url)

            embed.set_footer(text=f"User ID: {user.id}")

            # Attempt to find the moderator responsible via audit logs
            try:
                async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.unban):
                    if entry.target.id == user.id and (discord.utils.utcnow() - entry.created_at).total_seconds() < 20:
                        embed.add_field(name="Unbanned By", value=f"{entry.user.mention} ({entry.user.name})", inline=False)
                        if entry.reason:
                            embed.add_field(name="Reason", value=entry.reason, inline=False)
                        break
            except discord.Forbidden:
                pass  # Bot lacks permission to view audit logs

            await self.log_event(guild, embed=embed)

        except Exception as e:
            print(f"[Unban Log] Failed to log unban for {user}: {e}")

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
