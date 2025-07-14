import discord
from discord.ext import commands
from modules.utils import mysql
from modules.detection import nsfw
from modules.moderation import strike
from modules.utils.discord_utils import safe_get_channel, safe_get_member

class AggregatedModerationCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def handle_message(self, message: discord.Message):
        if message.author.bot:
            return
        
        if message.guild is None:
            return

        guild_id = message.guild.id

        if (message.channel.id not in await mysql.get_settings(guild_id, "exclude-channels")):
            if await nsfw.is_nsfw(bot=self.bot, 
                                  message=message, 
                                  nsfw_callback=nsfw.handle_nsfw_content, 
                                  guild_id=guild_id):
                try:
                    await message.channel.send(
                        f"{message.author.mention}, your message was detected to contain explicit content and was removed."
                    )
                except (discord.Forbidden, discord.NotFound):
                    print("Cannot delete message or message no longer exists.")


    @commands.Cog.listener()
    async def on_reaction_add(self,
                              reaction: discord.Reaction,
                              user:     discord.User | discord.Member):
        if user.bot or not reaction.emoji.is_custom_emoji():
            return

        # Most likely scanned it already, skip
        if reaction.count > 1:
            return

        await self._scan_emoji(
            guild = reaction.message.guild,
            message = reaction.message,
            emoji = reaction.emoji,
            member = user,
        )

    # Fallback for when message isn't cached
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.member and payload.member.bot:
            return

        # If cached, was scanned in on_reaction_add, skip
        cached_msg = self.bot._connection._get_message(payload.message_id)
        if cached_msg is not None:
            return

        try:
            channel = await safe_get_channel(self.bot, payload.channel_id)
            message = await channel.fetch_message(payload.message_id)
        except discord.NotFound:
            return
        except discord.HTTPException as e:
            print(f"[raw] fetch failed: {e}")
            return

        guild = self.bot.get_guild(payload.guild_id)
        member = await safe_get_member(guild, payload.user_id)
        emoji = payload.emoji

        # Avoid scanning again
        for r in message.reactions:
            if r.emoji == emoji and r.count > 1:
                return

        await self._scan_emoji(
            guild = guild,
            message = message,
            emoji = emoji,
            member = member,
        )

    async def _scan_emoji(
        self,
        *,
        guild: discord.Guild,
        message: discord.Message,
        emoji: discord.Emoji | discord.PartialEmoji,
        member: discord.Member | discord.User | None
    ) -> None:
        if not guild or not emoji.is_custom_emoji():
            return

        flagged = await nsfw.is_nsfw(
            bot = self.bot,
            url = str(emoji.url),
            member = member,
            guild_id = guild.id,
            nsfw_callback = nsfw.handle_nsfw_content,
        )
        if not flagged:
            return

        try:
            await message.remove_reaction(emoji, member)
        except discord.Forbidden:
            print("[emoji] lacking permissions to remove reaction")
        except discord.HTTPException as e:
            print(f"[emoji] failed to remove reaction: {e}")

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        await self._handle_member_avatar(member.guild, member, is_join=True)

    @commands.Cog.listener()
    async def on_user_update(self, before: discord.User, after: discord.User):
        if before.avatar == after.avatar:
            return

        for guild in self.bot.guilds:
            if not await mysql.get_settings(guild.id, "check-pfp"):
                continue 

            member = guild.get_member(after.id)
            if member is None:
                try:
                    member = await guild.fetch_member(after.id)
                except discord.NotFound:
                    continue

            await self._handle_member_avatar(guild, member)

    async def _handle_member_avatar(self, guild: discord.Guild, member: discord.Member, is_join: bool = False):
        if not await mysql.get_settings(guild.id, "check-pfp"):
            return

        avatar_url = member.avatar.url if member.avatar else None
        if not avatar_url:
            return

        is_nsfw = await nsfw.is_nsfw(
            self.bot,
            url=avatar_url,
            member=member,
            guild_id=guild.id,
        )

        if is_nsfw:
            action = await mysql.get_settings(guild.id, "nsfw-pfp-action")
            message = await mysql.get_settings(guild.id, "nsfw-pfp-message")
            await strike.perform_disciplinary_action(member, self.bot, action, message, source="pfp")
            # Send message
            if message:
                try:
                    await member.send(message)
                except discord.Forbidden:
                    print(f"[PFP] Cannot send message to {member.display_name}. User may have DMs disabled.")
        else:
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
                    await member.edit(timed_out_until=None, reason="Profile picture updated to a safe image.")
                    await mysql.execute_query(
                        "DELETE FROM timeouts WHERE user_id=%s AND guild_id=%s AND source='pfp'",
                        (member.id, guild.id)
                    )
                    print(f"[PFP] Cleared timeout for {member.display_name}")
                except discord.Forbidden:
                    print(f"[PFP] Missing permission to untimeout {member.display_name}")
                except discord.HTTPException as e:
                    if e.code != 50035:
                        print(f"[PFP] Failed to untimeout {member.display_name}: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(AggregatedModerationCog(bot))
