import asyncio
import discord
from discord.ext import commands
import time
from collections import defaultdict
from difflib import SequenceMatcher
from modules.utils import mysql
from modules.detection import nsfw
from modules.moderation import strike
from modules.utils.discord_utils import safe_get_user

class AggregatedModerationCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.user_message_cache = defaultdict(list)
        self.AGGREGATION_WINDOW = 10  # seconds
        self.DIFFERENCE_THRESHOLD = 0.7  # for edits

    async def handle_deletion(self, messages: list):
        for msg in messages:
            try:
                await msg.delete()
            except (discord.Forbidden, discord.NotFound):
                print(f"Cannot delete message (ID={msg.id}).")

    async def check_and_delete_if_offensive(self, message_content: str, messages_to_delete: list, guild_id: str) -> bool:
        category = await nsfw.moderator_api(text=message_content, guild_id=guild_id)
        if category:
            await self.handle_deletion(messages_to_delete)
            return True
        return False

    async def should_perform_check(self, user_id, guild_id):
        delete_offensive = await mysql.get_settings(guild_id, "delete-offensive")
        restrict_users = await mysql.get_settings(guild_id, "restrict-striked-users")
        has_strike = await mysql.get_strike_count(user_id, guild_id) > 0
        return delete_offensive or (has_strike and restrict_users)

    async def handle_message(self, message: discord.Message):
        if message.author.bot:
            return

        user_id = message.author.id
        guild_id = message.guild.id
        now = time.time()

        if (
            await mysql.get_settings(guild_id, "delete-nsfw") is True
            and message.channel.id not in await mysql.get_settings(guild_id, "exclude-channels")
        ):
            if await nsfw.is_nsfw(self.bot, message=message, nsfw_callback=nsfw.handle_nsfw_content):
                try:
                    await message.delete()
                    await message.channel.send(
                        f"{message.author.mention}, your message was detected to contain explicit content and was removed."
                    )
                except (discord.Forbidden, discord.NotFound):
                    print("Cannot delete message or message no longer exists.")
            else:
                print(f"is_nfw returned False for {message.author}'s message.")
        else:
            print(f"Skipping check for {message.author}'s message.")


        if await self.should_perform_check(user_id, guild_id):
            self.user_message_cache.setdefault(user_id, [])
            if len(message.content) <= 10:
                self.user_message_cache[user_id].append((now, message))

            self.user_message_cache[user_id] = [
                (t, m)
                for t, m in self.user_message_cache[user_id]
                if now - t < self.AGGREGATION_WINDOW
            ]

            messages_to_check = [m.content for _, m in self.user_message_cache[user_id]] or [message.content]
            combined_content = " ".join(messages_to_check)
            cached_messages = [msg for _, msg in self.user_message_cache[user_id]]
            messages_to_delete = cached_messages if cached_messages else [message]

            was_deleted = await self.check_and_delete_if_offensive(
                combined_content, messages_to_delete, guild_id
            )

            if was_deleted:
                self.user_message_cache[user_id].clear()

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if before.author.bot or before.content == after.content:
            return

        guild_id = after.guild.id
        user_id = after.author.id

        old_message = before.content
        new_message = after.content

        if await self.should_perform_check(user_id, guild_id):
            similarity_ratio = SequenceMatcher(None, old_message, new_message).ratio()
            if similarity_ratio < self.DIFFERENCE_THRESHOLD:
                await self.check_and_delete_if_offensive(new_message, [after], guild_id)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        await self._handle_member_avatar(member.guild, member, is_join=True)

    @commands.Cog.listener()
    async def on_user_update(self, before: discord.User, after: discord.User):
        if before.avatar != after.avatar and after.avatar:
            for guild in self.bot.guilds:
                member = await safe_get_user(self.bot, after.id)
                if member:
                    await self._handle_member_avatar(guild, member)

    async def _handle_member_avatar(self, guild: discord.Guild, member: discord.Member, is_join: bool = False):
        if await mysql.get_settings(guild.id, "check-pfp") != True:
            return

        avatar_url = member.avatar.url if member.avatar else None
        if not avatar_url:
            return

        is_nsfw = await nsfw.is_nsfw(
            self.bot,
            url=avatar_url,
            member=member,
            nsfw_callback=nsfw.handle_nsfw_content
        )

        if is_nsfw:
            action = await mysql.get_settings(guild.id, "nsfw-pfp-action")
            message = await mysql.get_settings(guild.id, "nsfw-pfp-message")
            await strike.perform_disciplinary_action(member, self.bot, action, message, source="pfp")
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
            if await mysql.get_settings(guild.id, "unmute-on-safe-pfp") == True and result is not None:
                if member.timed_out_until:
                    try:
                        await member.edit(timeout=None, reason="Profile picture updated to a safe image.")
                        print(f"Removed timeout from {member.display_name} for safe profile picture.")

                        await mysql.execute_query(
                            "DELETE FROM timeouts WHERE user_id = %s AND guild_id = %s",
                            (member.id, guild.id)
                        )
                    except discord.Forbidden:
                        print(f"Missing permissions to untimeout {member.display_name}.")
                    except discord.HTTPException as e:
                        print(f"Failed to untimeout {member.display_name}: {e}")

async def setup(bot: commands.Bot):
    await bot.add_cog(AggregatedModerationCog(bot))
