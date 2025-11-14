from __future__ import annotations

import time

import discord

from modules.cache import CachedMessage
from modules.moderation import strike
from modules.nsfw_scanner import handle_nsfw_content
from modules.nsfw_scanner.settings_keys import (
    NSFW_TEXT_ENABLED_SETTING,
    NSFW_TEXT_EXCLUDED_CHANNELS_SETTING,
)
from modules.utils import mod_logging, mysql
from modules.utils.discord_utils import safe_get_channel, safe_get_member, safe_get_message


class ModerationHandlers:
    def __init__(self, *, bot, scanner, enqueue_task):
        self._bot = bot
        self._scanner = scanner
        self._enqueue = enqueue_task
        self._video_extensions = {
            ".mp4",
            ".mov",
            ".avi",
            ".mkv",
            ".webm",
            ".mpeg",
            ".mpg",
            ".m4v",
            ".gifv",
        }

    async def _nsfw_enabled(self, guild_id: int) -> bool:
        return bool(await mysql.get_settings(guild_id, "nsfw-enabled"))

    async def handle_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return

        guild_id = message.guild.id
        nsfw_enabled = await self._nsfw_enabled(guild_id)
        text_scanning_enabled = bool(
            await mysql.get_settings(guild_id, NSFW_TEXT_ENABLED_SETTING)
        )
        text_excluded_channels = await mysql.get_settings(
            guild_id, NSFW_TEXT_EXCLUDED_CHANNELS_SETTING
        ) or []
        try:
            normalized_text_excluded = {int(cid) for cid in text_excluded_channels}
        except (TypeError, ValueError):
            normalized_text_excluded = {
                int(str(cid))
                for cid in text_excluded_channels
                if str(cid).isdigit()
            }
        text_scanning_allowed = (
            text_scanning_enabled and message.channel.id not in normalized_text_excluded
        )

        if not nsfw_enabled and not text_scanning_allowed:
            return

        scan_age_restricted = await mysql.get_settings(guild_id, "scan-age-restricted")

        chan = message.channel
        parent = getattr(chan, "parent", None)
        is_age_restricted = (
            (hasattr(chan, "is_nsfw") and chan.is_nsfw())
            or (parent is not None and hasattr(parent, "is_nsfw") and parent.is_nsfw())
        )
        if is_age_restricted and not scan_age_restricted:
            return

        excluded = await mysql.get_settings(guild_id, "exclude-channels") or []
        try:
            normalized_excluded = {int(c) for c in excluded}
        except (TypeError, ValueError):
            normalized_excluded = {
                int(str(c)) for c in excluded if str(c).isdigit()
            }
        if message.channel.id in normalized_excluded:
            return

        has_video = self._message_has_video_attachment(message)

        async def scan_task():
            scan_outcome = await self._scanner.is_nsfw(
                message=message,
                guild_id=guild_id,
                nsfw_callback=handle_nsfw_content,
                overall_started_at=queue_started_at,
                scan_text=text_scanning_allowed,
                scan_media=nsfw_enabled,
                return_details=True,
            )
            if not scan_outcome["flagged"]:
                return

            if scan_outcome.get("text_flagged"):
                return

            notify_channel = await mysql.get_settings(guild_id, "nsfw-channel-notify")
            if not notify_channel:
                return
            try:
                nsfw_texts = self._bot.translate(
                    "cogs.aggregated_moderation.nsfw_detection",
                    placeholders={"mention": message.author.mention},
                    guild_id=guild_id,
                )
                embed = discord.Embed(
                    title=nsfw_texts["title"],
                    description=nsfw_texts["description"],
                    color=discord.Color.red(),
                )
                embed.set_thumbnail(url=message.author.display_avatar.url)
                await mod_logging.log_to_channel(
                    embed=embed,
                    channel_id=message.channel.id,
                    bot=self._bot,
                )
            except (discord.Forbidden, discord.NotFound):
                print("[NSFW] Could not notify user about message removal.")

        queue_started_at = time.perf_counter()
        task_kind = "video" if has_video else None
        await self._enqueue(scan_task(), guild_id=guild_id, task_kind=task_kind)

    async def handle_message_edit(self, cached_before: CachedMessage, after: discord.Message) -> None:
        if after.author.bot or after.guild is None:
            return

        before_content = getattr(cached_before, "content", None)
        if before_content is not None and before_content == after.content:
            return

        await self.handle_message(after)

    async def handle_reaction_add(self, reaction: discord.Reaction, user: discord.User | discord.Member) -> None:
        guild = reaction.message.guild
        if guild is None:
            return
        if not await self._nsfw_enabled(guild.id):
            return
        if not isinstance(reaction.emoji, (discord.Emoji, discord.PartialEmoji)):
            return
        if reaction.count > 1:
            return

        await self._queue_emoji_scan(
            guild=guild,
            message=reaction.message,
            emoji=reaction.emoji,
            member=user,
            user_id=getattr(user, "id", None),
        )

    async def handle_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.member and payload.member.bot:
            return
        if not payload.emoji.is_custom_emoji():
            return

        cached_msg = self._bot._connection._get_message(payload.message_id)
        if cached_msg is not None:
            return

        try:
            channel = await safe_get_channel(self._bot, payload.channel_id)
            if channel is None:
                print(f"[raw] missing channel for reaction add {payload.channel_id}")
                return
            message = await safe_get_message(channel, payload.message_id)
        except discord.NotFound:
            return
        except discord.HTTPException as exc:
            print(f"[raw] fetch failed: {exc}")
            return

        if message is None:
            print(f"[raw] missing message {payload.message_id}; skipping reaction scan")
            return

        guild = self._bot.get_guild(payload.guild_id)
        if guild is None:
            return
        if not await self._nsfw_enabled(guild.id):
            return

        member = await safe_get_member(guild, payload.user_id)
        emoji = payload.emoji

        reactions = getattr(message, "reactions", None)
        if reactions is None:
            print(f"[raw] message {payload.message_id} missing reactions data; skipping reaction scan")
            return

        for existing in reactions:
            if str(existing.emoji) == str(emoji) and existing.count > 1:
                return

        await self._queue_emoji_scan(
            guild=guild,
            message=message,
            emoji=emoji,
            member=member,
            user_id=payload.user_id,
        )

    async def handle_member_join(self, member: discord.Member) -> None:
        await self._queue_avatar_scan(member.guild, member, is_join=True)

    async def handle_user_update(self, before: discord.User, after: discord.User) -> None:
        if before.avatar == after.avatar:
            return

        for guild in self._bot.guilds:
            member = await safe_get_member(guild, after.id)
            if member:
                await self._queue_avatar_scan(guild, member)

    async def _queue_emoji_scan(
        self,
        *,
        guild: discord.Guild,
        message: discord.Message,
        emoji: discord.Emoji | discord.PartialEmoji,
        member: discord.Member | discord.User | None,
        user_id: int | None = None,
    ) -> None:
        async def resolve_message_for_removal(msg):
            if msg is None:
                return None
            if hasattr(msg, "remove_reaction"):
                return msg

            channel = getattr(msg, "channel", None)
            channel_id = getattr(channel, "id", None) or getattr(msg, "channel_id", None)
            message_id = getattr(msg, "id", None) or getattr(msg, "message_id", None)
            if channel_id is None or message_id is None:
                return None

            if channel is None:
                channel = self._bot.get_channel(channel_id)
            if channel is None:
                channel = await safe_get_channel(self._bot, channel_id)
            if channel is None:
                return None

            try:
                return await channel.fetch_message(message_id)
            except (discord.NotFound, discord.Forbidden):
                return None
            except discord.HTTPException as exc:
                print(f"[emoji] failed to refetch message {message_id}: {exc}")
                return None

        async def scan_task():
            resolved_member = member
            if resolved_member is None and user_id is not None:
                resolved_member = await safe_get_member(guild, user_id)

            flagged = await self._scanner.is_nsfw(
                message=message,
                guild_id=guild.id,
                nsfw_callback=handle_nsfw_content,
                url=str(emoji.url),
                member=resolved_member,
                overall_started_at=queue_started_at,
            )
            if not flagged:
                return

            target_message = await resolve_message_for_removal(message)
            if target_message is None:
                print("[emoji] unable to resolve message for reaction removal")
                return

            target_member = member or (discord.Object(id=user_id) if user_id is not None else None)
            if target_member is None:
                print("[emoji] unable to resolve member for reaction removal")
                return

            try:
                await target_message.remove_reaction(emoji, target_member)
            except discord.Forbidden:
                print("[emoji] lacking permissions to remove reaction")
            except discord.HTTPException as exc:
                print(f"[emoji] failed to remove reaction: {exc}")

        queue_started_at = time.perf_counter()
        await self._enqueue(scan_task(), guild_id=guild.id)

    async def _queue_avatar_scan(self, guild: discord.Guild, member: discord.Member, is_join: bool = False) -> None:
        if not await self._nsfw_enabled(guild.id):
            return
        if not await mysql.get_settings(guild.id, "check-pfp"):
            return

        avatar_url = member.avatar.url if member.avatar else None
        if not avatar_url:
            return

        async def scan_task():
            is_nsfw = await self._scanner.is_nsfw(
                url=avatar_url,
                member=member,
                guild_id=guild.id,
                overall_started_at=queue_started_at,
            )
            if is_nsfw:
                action = await mysql.get_settings(guild.id, "nsfw-pfp-action")
                message = await mysql.get_settings(guild.id, "nsfw-pfp-message")
                await strike.perform_disciplinary_action(member, self._bot, action, message, source="pfp")
                if message:
                    try:
                        await member.send(message)
                    except discord.Forbidden:
                        print(f"[PFP] Cannot DM {member.display_name}.")
                return

            result, _ = await mysql.execute_query(
                """
                SELECT timeout_until FROM timeouts
                WHERE user_id = %s AND guild_id = %s
                AND timeout_until > UTC_TIMESTAMP()
                AND source = 'pfp'
                """,
                (member.id, guild.id),
                fetch_one=True,
            )
            if await mysql.get_settings(guild.id, "unmute-on-safe-pfp") and result is not None:
                try:
                    await member.edit(
                        timed_out_until=None,
                        reason=self._bot.translate(
                            "cogs.aggregated_moderation.pfp.safe_reason",
                            guild_id=guild.id,
                        ),
                    )
                    await mysql.execute_query(
                        "DELETE FROM timeouts WHERE user_id=%s AND guild_id=%s AND source='pfp'",
                        (member.id, guild.id),
                    )
                    print(f"[PFP] Cleared timeout for {member.display_name}")
                except discord.Forbidden:
                    print(f"[PFP] Missing permission to untimeout {member.display_name}")
                except discord.HTTPException as exc:
                    if exc.code != 50035:
                        print(f"[PFP] Failed to untimeout {member.display_name}: {exc}")

        queue_started_at = time.perf_counter()
        await self._enqueue(scan_task(), guild_id=guild.id)

    def _attachment_is_video(self, attachment: discord.Attachment) -> bool:
        content_type = (getattr(attachment, "content_type", None) or "").lower()
        if content_type.startswith("video/"):
            return True
        filename = (getattr(attachment, "filename", None) or "").lower()
        return any(filename.endswith(ext) for ext in self._video_extensions)

    def _message_has_video_attachment(self, message: discord.Message) -> bool:
        attachments = getattr(message, "attachments", None) or []
        for attachment in attachments:
            try:
                if self._attachment_is_video(attachment):
                    return True
            except Exception:
                continue
        return False


__all__ = ["ModerationHandlers"]
