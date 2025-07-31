from typing import Optional
from discord import Embed, Color, Interaction
import discord
from discord.ext import commands
from modules.cache import get_cached_message
from modules.utils import mysql
from discord.utils import format_dt, utcnow
from discord import app_commands

class MonitoringCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.invite_cache: dict[int, dict[str, int]] = {} # guild_id: {invite_code: uses}

    async def get_monitor_channel(self, guild_id: int) -> Optional[int]:
        id = await mysql.get_settings(guild_id, "monitor-channel")
        return int(id) if id else None

    async def is_event_enabled(self, guild_id: int, event_name: str) -> bool:
        settings = await mysql.get_settings(guild_id, "monitor-events") or {}
        return settings.get(event_name, True)

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
                invites = await guild.invites()
                self.invite_cache[guild.id] = {i.code: i.uses or 0 for i in invites}
            except Exception as e:
                print(f"Failed to fetch invites for {guild.name}: {e}")

    @commands.Cog.listener()
    async def on_invite_create(self, invite):
        self.invite_cache.setdefault(invite.guild.id, {})[invite.code] = invite.uses or 0

    @commands.Cog.listener()
    async def on_invite_delete(self, invite):
        self.invite_cache.get(invite.guild.id, {}).pop(invite.code, None)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        guild = member.guild
        gid = guild.id

        if not await self.is_event_enabled(gid, "join"):
            return

        inviter_reason = None
        used_invite = None

        try:
            new_invites = await guild.invites()
            
            # Look for the invite whose usage increased
            for invite in new_invites:
                old = self.invite_cache.get(gid, {}).get(invite.code, 0)
                if (invite.uses or 0) > old:
                    used_invite = invite
                    break

            # Update cache
            self.invite_cache[gid] = {i.code: i.uses or 0 for i in new_invites}

            if not used_invite:
                inviter_reason = "Invite not previously tracked or uses unchanged"

        except discord.Forbidden:
            inviter_reason = "Missing Manage Server permission"
            print(f"[Join Log] {inviter_reason} in {guild.name}")
        except Exception as e:
            inviter_reason = f"Error while fetching invites ({e})"
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

        if not await self.is_event_enabled(guild.id, "timeout"):
            return

        timed_out  = after.timed_out_until is not None
        title      = "Member Timed-Out" if timed_out else "Timeout Removed"
        colour     = discord.Color.dark_orange() if timed_out else discord.Color.green()

        moderator  = reason = None
        try:
            async for entry in guild.audit_logs(limit=6,
                                                action=discord.AuditLogAction.member_update):
                if entry.target.id != after.id:
                    continue
                if (utcnow() - entry.created_at).total_seconds() > 60:
                        break

                before_cd = getattr(entry.changes.before, "timed_out_until", None)
                after_cd = getattr(entry.changes.after, "timed_out_until", None)

                if timed_out and after_cd:
                    moderator, reason = entry.user, entry.reason
                    break
                if not timed_out and before_cd and after_cd is None:
                    moderator, reason = entry.user, entry.reason
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

        embed.set_thumbnail(url=after.display_avatar.url)

        if timed_out:
            ts = int(after.timed_out_until.timestamp())
            embed.add_field(name="Ends", value=f"<t:{ts}:F>  (<t:{ts}:R>)")

        if moderator:
            embed.add_field(name="Moderator",
                            value=f"{moderator.mention} ({moderator.name})",
                            inline=False)
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

            if kicked: 
                if not await self.is_event_enabled(guild.id, "kick"):
                    return
            else:
                if not await self.is_event_enabled(guild.id, "leave"):
                    return

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

    async def handle_message_delete(self, cached_message: dict):
        # Check if the event is enabled for this guild
        cached_guild_id = cached_message.get("guild_id")
        if not await self.is_event_enabled(cached_guild_id, "message_delete"):
            return

        # Get cached message details
        cached_message_content = cached_message.get("content")
        cached_user_id = cached_message.get("author_id")
        cached_user_mention = cached_message.get("author_mention")
        cached_user_name = cached_message.get("author_name")
        cached_embeds = cached_message.get("embeds", [])
        cached_attachments = cached_message.get("attachments", [])
        cached_stickers = cached_message.get("stickers", [])
        channel = self.bot.get_channel(cached_message["channel_id"])

        deleter: str | None = None
        try:
            async for entry in channel.guild.audit_logs(
                action=discord.AuditLogAction.message_delete,
                limit=5
            ):
                if (
                    entry.extra.channel.id == channel.id and
                    entry.target.id == cached_user_id and
                    (utcnow() - entry.created_at).total_seconds() < 20
                ):
                    deleter = f"{entry.user.mention} ({entry.user.name})"
                    break
        except discord.Forbidden:
            pass
        except Exception as e:
            print(f"[Audit-log lookup] {e}")

        if cached_message and cached_message_content:
            embed = Embed(
                title="Message Deleted",
                description=(
                    f"**Author:** {cached_user_mention} ({cached_user_name})\n"
                    f"**Channel:** {channel.mention}\n"
                    + (f"**Deleted by:** {deleter}\n" if deleter else "")
                    + f"**Content:**\n{cached_message_content}"
                ),
                color=Color.orange()
            )
            embed.set_footer(text=f"User ID: {cached_user_id}")

            # Add cached attachments
            if cached_attachments:
                links = [f"• [{a['filename']}]({a['url']})" for a in cached_attachments]
                embed.add_field(
                    name=f"Attachments ({len(links)})",
                    value="\n".join(links)[:1024],
                    inline=False
                )

            # Add cached embeds
            if cached_embeds:
                for i, embed_dict in enumerate(cached_embeds):
                    try:
                        rich_embed = Embed.from_dict(embed_dict)
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

            # Add cached stickers
            if cached_stickers:
                try:
                    links = []
                    for sticker in cached_stickers:
                        links.append(f"• [{sticker['name']}]({sticker['url']})")

                    embed.add_field(
                        name=f"Stickers ({len(links)})",
                        value="\n".join(links)[:1024],
                        inline=False
                    )
                except Exception as e:
                    print(f"[Sticker image parse fail] {e}")
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
        if not await self.is_event_enabled(guild.id, "ban"):
            return
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
        if not await self.is_event_enabled(guild.id, "unban"):
            return
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

    async def handle_message_edit(self, cached_before: dict, after: discord.Message):
        if not await self.is_event_enabled(after.guild.id, "message_edit"):
            return

        embed = Embed(
            title="Message Edited",
            description=(
                f"**Author:** {cached_before['author_mention']} ({cached_before['author_name']})\n"
                f"**Channel:** {after.channel.mention}\n"
            ),
            color=Color.gold()
        )

        embed.add_field(name="Before", value=cached_before.get("content") or "[No Content]", inline=False)
        embed.add_field(name="After", value=after.content or "[No Content]", inline=False)
        embed.set_footer(text=f"User ID: {after.author.id}")

        await self.log_event(after.guild, embed=embed, mention_user=False)

    monitor_group = app_commands.Group(
        name="monitor",
        description="Monitoring configuration",
        guild_only=True,
        default_permissions=discord.Permissions(manage_messages=True),
    )

    @monitor_group.command(name="set", description="Set channel to output logs.")
    @app_commands.describe(channel="The channel to send logs to.")
    async def monitor_set(self, interaction: Interaction, channel: discord.TextChannel):
        await mysql.update_settings(interaction.guild.id, "monitor-channel", channel.id)
        await interaction.response.send_message(f"Monitor channel set to {channel.mention}.", ephemeral=True)

    @monitor_group.command(name="remove", description="Remove the monitor channel setting.")
    async def monitor_remove(self, interaction: Interaction):
        removed = await mysql.update_settings(interaction.guild.id, "monitor-channel", None)
        if removed:
            await interaction.response.send_message("Monitor channel has been removed.", ephemeral=True)
        else:
            await interaction.response.send_message("No monitor channel was set.", ephemeral=True)

    @monitor_group.command(name="show", description="Show the current monitor channel.")
    async def monitor_show(self, interaction: Interaction):
        channel_id = await mysql.get_settings(interaction.guild.id, "monitor-channel")
        if channel_id:
            channel = interaction.guild.get_channel(channel_id)
            mention = channel.mention if channel else f"`#{channel_id}` (not found)"
            await interaction.response.send_message(f"Current monitor channel: {mention}", ephemeral=True)
        else:
            await interaction.response.send_message("No monitor channel is currently set.", ephemeral=True)
            
    @monitor_group.command(name="toggle_event", description="Enable or disable a specific monitoring event.")
    @app_commands.describe(event="The event to toggle", enabled="Enable or disable logging for this event")
    @app_commands.choices(event=[
        app_commands.Choice(name="User Join", value="join"),
        app_commands.Choice(name="User Leave", value="leave"),
        app_commands.Choice(name="Ban", value="ban"),
        app_commands.Choice(name="Unban", value="unban"),
        app_commands.Choice(name="Timeout", value="timeout"),
        app_commands.Choice(name="Message Deleted", value="message_delete"),
        app_commands.Choice(name="Message Edited", value="message_edit"),
    ])
    async def toggle_event(self, interaction: Interaction, event: app_commands.Choice[str], enabled: bool):
        settings = await mysql.get_settings(interaction.guild.id, "monitor-events") or {}
        settings[event.value] = enabled
        await mysql.update_settings(interaction.guild.id, "monitor-events", settings)
        await interaction.response.send_message(
            f"Logging for `{event.name}` has been {'enabled' if enabled else 'disabled'}.", ephemeral=True
        )

    @monitor_group.command(name="list_events", description="List current monitor event settings.")
    async def list_events(self, interaction: Interaction):
        await interaction.response.defer(ephemeral=True)
        settings = await mysql.get_settings(interaction.guild.id, "monitor-events") or {}
        lines = [f"- `{k}`: {'✅' if v else '❌'}" for k, v in settings.items()]
        await interaction.followup.send("**Monitor Events:**\n" + "\n".join(lines), ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(MonitoringCog(bot))
