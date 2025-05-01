import discord
from discord.ext import commands
import time
from collections import defaultdict
from difflib import SequenceMatcher
from modules.utils import mysql
from modules.detection import nsfw

class AggregatedModeration(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.user_message_cache = defaultdict(list)
        self.AGGREGATION_WINDOW = 10  # seconds
        self.DIFFERENCE_THRESHOLD = 0.7  # for edits

    async def handle_deletion(self, messages: list):
        """
        Safely delete a list of discord.Message objects.
        """
        for msg in messages:
            try:
                await msg.delete()
            except (discord.Forbidden, discord.NotFound):
                print(f"Cannot delete message (ID={msg.id}).")

    async def check_and_delete_if_offensive(self, message_content: str, messages_to_delete: list, guild_id:str) -> bool:
        """
        Check if 'content' is offensive. If it is, delete all 'messages_to_delete'.
        Returns True if deleted (i.e., was offensive), else False.
        """
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
    
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        guild_id = message.guild.id
        user_id = message.author.id
        now = time.time()

        # Offensive text checks (aggregation logic)
        if await self.should_perform_check(user_id, guild_id):
            # Maintain a rolling window of short messages
            self.user_message_cache.setdefault(user_id, [])
            if len(message.content) <= 10:
                self.user_message_cache[user_id].append((now, message))

            # Remove old messages from the cache
            self.user_message_cache[user_id] = [
                (t, m)
                for t, m in self.user_message_cache[user_id]
                if now - t < self.AGGREGATION_WINDOW
            ]

            # Create a combined view of recent short messages
            messages_to_check = [m.content for _, m in self.user_message_cache[user_id]] or [message.content]
            combined_content = " ".join(messages_to_check)

            # Prepare the list of messages to delete if offensive
            cached_messages = [msg for _, msg in self.user_message_cache[user_id]]
            messages_to_delete = cached_messages if cached_messages else [message]

            was_deleted = await self.check_and_delete_if_offensive(
                combined_content, messages_to_delete, guild_id
            )

            # If flagged, clear the cache for this user
            if was_deleted:
                self.user_message_cache[user_id].clear()

        # NSFW (non-text) checks
        if (
            await mysql.get_settings(guild_id, "delete-nsfw") is True
            and message.channel.id not in await mysql.get_settings(guild_id, "exclude-channels")
        ):
            if await nsfw.is_nsfw(message, self.bot, nsfw.handle_nsfw_content):
                try:
                    await message.delete()
                    await message.channel.send(
                        f"{message.author.mention}, your message was detected to contain explicit content and was removed."
                    )
                except (discord.Forbidden, discord.NotFound):
                    print("Cannot delete message or message no longer exists.")

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        # Only proceed if content changed and it's not from a bot
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
        # Check pfp for NSFW content
        if await mysql.get_settings(member.guild.id, "check-pfp") == True:
            if await nsfw.is_nsfw(member.avatar.url, self.bot, nsfw.handle_nsfw_content):
                try:
                    await member.kick(reason="NSFW profile picture detected.")
                    await member.send(
                        "Your profile picture was detected to contain explicit content and you have been removed from the server."
                    )
                except (discord.Forbidden, discord.NotFound):
                    print("Cannot kick member or member no longer exists.")


async def setup(bot: commands.Bot):
    await bot.add_cog(AggregatedModeration(bot))