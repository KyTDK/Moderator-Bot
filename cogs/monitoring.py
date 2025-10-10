from typing import Optional
from discord import Embed, Color, Interaction
import discord
from discord.ext import commands
from modules.cache import CachedMessage
from modules.utils import mysql
from discord.utils import format_dt, utcnow
from discord import app_commands
from modules.core.moderator_bot import ModeratorBot
from modules.i18n.strings import locale_string

class MonitoringCog(commands.Cog):
    def __init__(self, bot: ModeratorBot):
        self.bot = bot
        self._monitor_blocked_channels: set[int] = set()

    @staticmethod
    def _truncate_embed_value(value: str, limit: int = 1024) -> str:
        """Ensure embed field values stay within Discord's 1024 char limit."""
        if not value:
            return ""
        if len(value) <= limit:
            return value
        suffix = "... (truncated)"
        available = max(limit - len(suffix), 0)
        truncated = value[:available].rstrip()
        if not truncated:
            return suffix[:limit]
        return truncated + suffix

    def _texts(self, section: str, guild_id: int):
        return self.bot.translate(f"cogs.monitoring.{section}",
                                    guild_id=guild_id
                                  )

    async def get_monitor_channel(self, guild_id: int) -> Optional[int]:
        channel_id = await mysql.get_settings(guild_id, "monitor-channel")
        return int(channel_id) if channel_id else None

    async def is_event_enabled(self, guild_id: int, event_name: str) -> bool:
        settings = await mysql.get_settings(guild_id, "monitor-events") or {}
        return settings.get(event_name, True)

    async def log_event(
        self,
        guild: discord.Guild,
        message: Optional[str] = None,
        embed: Optional[discord.Embed] = None,
        mention_user: bool = True,
    ):
        channel_id = await self.get_monitor_channel(guild.id)
        if not channel_id:
            return

        channel = guild.get_channel(channel_id)
        if channel is None:
            return

        bot_user = self.bot.user
        if bot_user is None:
            print(f"Missing bot user when logging monitor event for guild {guild.id}")
            return

        me = guild.me or guild.get_member(bot_user.id)
        if me is None:
            print(f"Missing guild member record for bot in guild {guild.id}")
            return

        if hasattr(channel, "permissions_for"):
            perms = channel.permissions_for(me)
            if not perms.view_channel or not perms.send_messages:
                if channel.id not in self._monitor_blocked_channels:
                    print(f"Missing access to send messages in channel ID {channel.id} for guild {guild.id}")
                self._monitor_blocked_channels.add(channel.id)
                return

        try:
            allowed = discord.AllowedMentions.all() if mention_user else discord.AllowedMentions.none()
            log_texts = self._texts("log_event",
                                    guild_id=guild.id)
            if embed is not None:
                is_accelerated = await mysql.is_accelerated(guild_id=guild.id)
                if not is_accelerated:
                    embed.set_footer(text=log_texts["upgrade_footer"])
                else:
                    embed.set_footer(text=log_texts["guild_footer"].format(guild_id=guild.id))

            await channel.send(content=message, embed=embed, allowed_mentions=allowed)
            self._monitor_blocked_channels.discard(channel.id)
        except discord.Forbidden:
            if channel.id not in self._monitor_blocked_channels:
                print(f"Missing access to send messages in channel ID {channel.id} for guild {guild.id}")
            self._monitor_blocked_channels.add(channel.id)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        guild = member.guild
        if not await self.is_event_enabled(guild.id, "join"):
            return

        texts = self._texts("join",
                            guild_id=guild.id)
        try:
            embed = Embed(
                title=texts["title"],
                description=texts["description"].format(mention=member.mention, name=member.name),
                color=Color.green(),
            )

            avatar_url = member.avatar.url if member.avatar else member.default_avatar.url
            embed.set_thumbnail(url=avatar_url)

            embed.add_field(
                name=texts["fields"]["account_created"],
                value=format_dt(member.created_at or utcnow(), style="F"),
                inline=True,
            )
            embed.add_field(
                name=texts["fields"]["joined_at"],
                value=format_dt(member.joined_at or utcnow(), style="F"),
                inline=True,
            )

            footer_key = "bot" if member.bot else "user"
            embed.set_footer(text=texts["footer"][footer_key])
            await self.log_event(guild, embed=embed)
        except Exception as exc:
            print(f"[Join Log] Failed to send join embed for {member}: {exc}")

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if before.timed_out_until == after.timed_out_until:
            return

        guild = after.guild
        if not await self.is_event_enabled(guild.id, "timeout"):
            return

        texts = self._texts("timeout",
                            guild_id=guild.id)
        timed_out = after.timed_out_until is not None
        title = texts["title_applied"] if timed_out else texts["title_removed"]
        description_template = texts["description_applied"] if timed_out else texts["description_removed"]
        embed = Embed(
            title=title,
            description=description_template.format(mention=after.mention, name=after.name),
            color=discord.Color.dark_orange() if timed_out else discord.Color.green(),
        )
        embed.set_thumbnail(url=after.display_avatar.url)

        if timed_out and after.timed_out_until:
            ts = int(after.timed_out_until.timestamp())
            embed.add_field(name=texts["fields"]["ends"], value=f"<t:{ts}:F>  (<t:{ts}:R>)")

        moderator = None
        reason = None
        try:
            async for entry in guild.audit_logs(limit=6, action=discord.AuditLogAction.member_update):
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
        except Exception as exc:
            print(f"[Timeout Log] Audit-log lookup failed: {exc}")

        if moderator:
            embed.add_field(
                name=texts["fields"]["moderator"],
                value=f"{moderator.mention} ({moderator.name})",
                inline=False,
            )
        if reason:
            embed.add_field(name=texts["fields"]["reason"], value=reason, inline=False)

        embed.set_footer(text=texts["footer"].format(user_id=after.id))
        await self.log_event(guild, embed=embed)

    @commands.Cog.listener()
    async def on_raw_member_remove(self, payload: discord.RawMemberRemoveEvent):
        try:
            guild = self.bot.get_guild(payload.guild_id)
            if guild is None:
                return

            user_id = payload.user.id
            user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
            kicked = False
            kicker = None
            reason = None

            try:
                async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.kick):
                    if entry.target.id == user.id and (utcnow() - entry.created_at).total_seconds() < 20:
                        kicked = True
                        kicker = entry.user
                        reason = entry.reason
                        break
            except discord.Forbidden:
                print("[Kick Log] Missing permissions to view audit logs.")
            except Exception as exc:
                print(f"[Kick Log] Audit log lookup failed: {exc}")

            event_key = "kick" if kicked else "leave"
            if not await self.is_event_enabled(guild.id, event_key):
                return

            texts = self._texts("leave",
                                guild_id=guild.id)
            title = texts["title_kicked"] if kicked else texts["title_left"]
            description = (texts["description_kicked"] if kicked else texts["description_left"]).format(
                mention=user.mention,
                name=user.name,
            )
            embed = Embed(
                title=title,
                description=description,
                color=Color.orange() if kicked else Color.red(),
            )
            avatar_url = user.avatar.url if user.avatar else user.default_avatar.url
            embed.set_thumbnail(url=avatar_url)

            if kicked and kicker:
                embed.add_field(
                    name=texts["fields"]["kicked_by"],
                    value=f"{kicker.mention} ({kicker.name})",
                    inline=False,
                )
                if reason:
                    embed.add_field(name=texts["fields"]["reason"], value=reason, inline=False)

            footer_key = "bot" if user.bot else "user"
            embed.set_footer(text=texts["footer"][footer_key])
            await self.log_event(guild, embed=embed)
        except Exception as exc:
            print(f"[Leave Log] Failed to log member removal: {exc}")

    async def handle_message_delete(self, cached_message: CachedMessage):
        guild_id = cached_message.guild_id
        if not await self.is_event_enabled(guild_id, "message_delete"):
            return

        texts = self._texts("message_delete",
                            guild_id=guild_id)
        channel = self.bot.get_channel(cached_message.channel_id)
        if channel is None:
            return

        deleter = None
        try:
            async for entry in channel.guild.audit_logs(
                action=discord.AuditLogAction.message_delete,
                limit=5,
            ):
                if (
                    entry.extra.channel.id == channel.id
                    and entry.target.id == cached_message.author_id
                    and (utcnow() - entry.created_at).total_seconds() < 20
                ):
                    deleter = f"{entry.user.mention} ({entry.user.name})"
                    break
        except discord.Forbidden:
            pass
        except Exception as exc:
            print(f"[Audit-log lookup] {exc}")

        if any((cached_message.content, cached_message.embeds, cached_message.attachments, cached_message.stickers)):
            description = texts["description_with_author"].format(
                author_mention=cached_message.author_mention,
                author_name=cached_message.author_name,
                channel=channel.mention,
            )
            if deleter:
                description += texts["deleted_by"].format(deleter=deleter)
            description += texts["content_label"].format(
                content=cached_message.content or texts["no_content"]
            )
            embed = Embed(
                title=texts["title"],
                description=description,
                color=Color.orange(),
            )
            embed.set_footer(text=texts["footer"].format(user_id=cached_message.author_id))

            if cached_message.attachments:
                links = [
                    texts["attachment_item"].format(name=a.filename, url=a.url)
                    for a in cached_message.attachments
                ]
                embed.add_field(
                    name=texts["fields"]["attachments"].format(count=len(links)),
                    value="\n".join(links)[:1024],
                    inline=False,
                )

            if cached_message.embeds:
                for index, rich_embed in enumerate(cached_message.embeds, start=1):
                    try:
                        parts: list[str] = []
                        if rich_embed.title:
                            parts.append(f"**{rich_embed.title}**")
                        if rich_embed.description:
                            parts.append(rich_embed.description)
                        if rich_embed.fields:
                            for field in rich_embed.fields:
                                parts.append(
                                    texts["embed_bullet"].format(
                                        name=field.name,
                                        value=field.value,
                                    )
                                )
                        if rich_embed.url:
                            parts.append(f"[Link]({rich_embed.url})")
                        if rich_embed.author:
                            parts.append(f"_Author: {rich_embed.author.name}_")
                        if rich_embed.footer:
                            parts.append(f"_Footer: {rich_embed.footer.text}_")

                        summary = "\n".join(parts)[:1024]
                        embed.add_field(
                            name=texts["fields"]["embeds"].format(index=index),
                            value=summary or texts["embed_unavailable"],
                            inline=False,
                        )
                    except Exception as exc:
                        print(f"[Embed parse fail] {exc}")

            if cached_message.stickers:
                try:
                    links = [
                        texts["sticker_item"].format(name=sticker.name, url=sticker.url)
                        for sticker in cached_message.stickers
                    ]
                    embed.add_field(
                        name=texts["fields"]["stickers"].format(count=len(links)),
                        value="\n".join(links)[:1024],
                        inline=False,
                    )
                except Exception as exc:
                    print(f"[Sticker image parse fail] {exc}")
        else:
            description = texts["description_generic"].format(channel=channel.mention)
            if deleter:
                description += texts["description_deleted_by"].format(deleter=deleter)
            embed = Embed(
                title=texts["title"],
                description=description,
                color=Color.orange(),
            )
        await self.log_event(channel.guild, embed=embed, mention_user=False)

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User):
        if not await self.is_event_enabled(guild.id, "ban"):
            return
        texts = self._texts("ban",
                            guild_id=guild.id)
        try:
            embed = discord.Embed(
                title=texts["title"],
                description=texts["description"].format(mention=user.mention, name=user.name),
                color=discord.Color.dark_red(),
            )
            avatar_url = user.avatar.url if user.avatar else user.default_avatar.url
            embed.set_thumbnail(url=avatar_url)
            embed.set_footer(text=texts["footer"].format(user_id=user.id))

            try:
                async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.ban):
                    if entry.target.id == user.id and (discord.utils.utcnow() - entry.created_at).total_seconds() < 20:
                        embed.add_field(
                            name=texts["fields"]["moderator"],
                            value=f"{entry.user.mention} ({entry.user.name})",
                            inline=False,
                        )
                        if entry.reason:
                            embed.add_field(name=texts["fields"]["reason"], value=entry.reason, inline=False)
                        break
            except discord.Forbidden:
                pass

            await self.log_event(guild, embed=embed)
        except Exception as exc:
            print(f"[Ban Log] Failed to log ban for {user}: {exc}")

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User):
        if not await self.is_event_enabled(guild.id, "unban"):
            return
        texts = self._texts("unban",
                            guild_id=guild.id)
        try:
            embed = discord.Embed(
                title=texts["title"],
                description=texts["description"].format(mention=user.mention, name=user.name),
                color=discord.Color.green(),
            )
            avatar_url = user.avatar.url if user.avatar else user.default_avatar.url
            embed.set_thumbnail(url=avatar_url)
            embed.set_footer(text=texts["footer"].format(user_id=user.id))

            try:
                async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.unban):
                    if entry.target.id == user.id and (discord.utils.utcnow() - entry.created_at).total_seconds() < 20:
                        embed.add_field(
                            name=texts["fields"]["moderator"],
                            value=f"{entry.user.mention} ({entry.user.name})",
                            inline=False,
                        )
                        if entry.reason:
                            embed.add_field(name=texts["fields"]["reason"], value=entry.reason, inline=False)
                        break
            except discord.Forbidden:
                pass

            await self.log_event(guild, embed=embed)
        except Exception as exc:
            print(f"[Unban Log] Failed to log unban for {user}: {exc}")

    async def handle_message_edit(self, cached_before: CachedMessage, after: discord.Message):
        if not await self.is_event_enabled(after.guild.id, "message_edit"):
            return
        texts = self._texts("message_edit",
                            guild_id=after.guild.id)
        embed = Embed(
            title=texts["title"],
            description=texts["description"].format(
                author_mention=cached_before.author_mention,
                author_name=cached_before.author_name,
                channel=after.channel.mention,
            ),
            color=Color.gold(),
        )
        no_content = texts["no_content"]
        embed.add_field(
            name=texts["fields"]["before"],
            value=self._truncate_embed_value(cached_before.content or no_content),
            inline=False,
        )
        embed.add_field(
            name=texts["fields"]["after"],
            value=self._truncate_embed_value(after.content or no_content),
            inline=False,
        )
        embed.set_footer(text=texts["footer"].format(user_id=after.author.id))
        await self.log_event(after.guild, embed=embed, mention_user=False)

    monitor_group = app_commands.Group(
        name="monitor",
        description=locale_string("cogs.monitoring.meta.group_description"),
        guild_only=True,
        default_permissions=discord.Permissions(manage_messages=True),
    )

    @monitor_group.command(
        name="set",
        description=locale_string("cogs.monitoring.meta.set.description"),
    )
    @app_commands.describe(
        channel=locale_string("cogs.monitoring.meta.set.channel")
    )
    async def monitor_set(self, interaction: Interaction, channel: discord.TextChannel):
        guild_id = interaction.guild.id
        texts = self._texts("monitor_commands",
                            guild_id=guild_id)
        await mysql.update_settings(interaction.guild.id, "monitor-channel", channel.id)
        await interaction.response.send_message(
            texts["set"].format(channel=channel.mention),
            ephemeral=True,
        )

    @monitor_group.command(
        name="remove",
        description=locale_string("cogs.monitoring.meta.remove.description"),
    )
    async def monitor_remove(self, interaction: Interaction):
        guild_id = interaction.guild.id
        texts = self._texts("monitor_commands",
                            guild_id=guild_id)
        removed = await mysql.update_settings(interaction.guild.id, "monitor-channel", None)
        if removed:
            await interaction.response.send_message(texts["removed"], ephemeral=True)
        else:
            await interaction.response.send_message(texts["not_set"], ephemeral=True)

    @monitor_group.command(
        name="show",
        description=locale_string("cogs.monitoring.meta.show.description"),
    )
    async def monitor_show(self, interaction: Interaction):
        guild_id = interaction.guild.id
        texts = self._texts("monitor_commands",
                            guild_id=guild_id)
        channel_id = await mysql.get_settings(interaction.guild.id, "monitor-channel")
        if channel_id:
            channel = interaction.guild.get_channel(channel_id)
            mention = channel.mention if channel else f"`#{channel_id}` (not found)"
            await interaction.response.send_message(
                texts["current"].format(channel=mention),
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(texts["none"], ephemeral=True)

    @monitor_group.command(
        name="toggle_event",
        description=locale_string("cogs.monitoring.meta.toggle_event.description"),
    )
    @app_commands.describe(
        event=locale_string("cogs.monitoring.meta.toggle_event.event"),
        enabled=locale_string("cogs.monitoring.meta.toggle_event.enabled"),
    )
    @app_commands.choices(
        event=[
            app_commands.Choice(
                name=locale_string("cogs.monitoring.meta.toggle_event.choices.join"),
                value="join",
            ),
            app_commands.Choice(
                name=locale_string("cogs.monitoring.meta.toggle_event.choices.leave"),
                value="leave",
            ),
            app_commands.Choice(
                name=locale_string("cogs.monitoring.meta.toggle_event.choices.ban"),
                value="ban",
            ),
            app_commands.Choice(
                name=locale_string("cogs.monitoring.meta.toggle_event.choices.unban"),
                value="unban",
            ),
            app_commands.Choice(
                name=locale_string("cogs.monitoring.meta.toggle_event.choices.timeout"),
                value="timeout",
            ),
            app_commands.Choice(
                name=locale_string("cogs.monitoring.meta.toggle_event.choices.message_delete"),
                value="message_delete",
            ),
            app_commands.Choice(
                name=locale_string("cogs.monitoring.meta.toggle_event.choices.message_edit"),
                value="message_edit",
            ),
        ]
    )
    async def toggle_event(self, interaction: Interaction, event: app_commands.Choice[str], enabled: bool):
        guild_id = interaction.guild.id
        texts = self._texts("monitor_commands",
                            guild_id=guild_id)
        settings = await mysql.get_settings(interaction.guild.id, "monitor-events") or {}
        settings[event.value] = enabled
        await mysql.update_settings(interaction.guild.id, "monitor-events", settings)
        state = texts["state_enabled"] if enabled else texts["state_disabled"]
        await interaction.response.send_message(
            texts["toggle"].format(event=event.name, state=state),
            ephemeral=True,
        )

    @monitor_group.command(
        name="list_events",
        description=locale_string("cogs.monitoring.meta.list_events.description"),
    )
    async def list_events(self, interaction: Interaction):
        guild_id = interaction.guild.id
        texts = self._texts("monitor_commands",
                            guild_id=guild_id)
        await interaction.response.defer(ephemeral=True)
        settings = await mysql.get_settings(interaction.guild.id, "monitor-events") or {}
        lines = []
        for key, value in settings.items():
            state_symbol = texts["list_enabled"] if value else texts["list_disabled"]
            lines.append(texts["list_item"].format(event=key, state=state_symbol))
        body = texts["list_heading"] + "\n" + "\n".join(lines)
        await interaction.followup.send(body, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(MonitoringCog(bot))
