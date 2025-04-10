import discord
from discord.ext import commands
import time
from collections import defaultdict

# Replace these with your actual implementations or imports
from modules.utils import mysql
from modules.detection import nsfw

class AggregatedModeration(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # Cache to store recent messages by user (user_id: list of (timestamp, message))
        self.user_message_cache = defaultdict(list)
        self.AGGREGATION_WINDOW = 10  # seconds

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        guild_id = message.guild.id
        user_id = message.author.id
        now = time.time()

        # Retrieve settings only once
        delete_offensive = mysql.get_settings(guild_id, "delete-offensive")
        restrict_users = mysql.get_settings(guild_id, "restrict-striked-users")
        has_strike = mysql.get_strike_count(user_id, guild_id) > 0

        if delete_offensive or (has_strike and restrict_users):
            # Initialize user cache if it doesn't exist
            self.user_message_cache.setdefault(user_id, [])

            if len(message.content) <= 10:
                self.user_message_cache[user_id].append((now, message))

            # Remove old messages from cache
            self.user_message_cache[user_id] = [
                (t, m) for t, m in self.user_message_cache[user_id] if now - t < self.AGGREGATION_WINDOW
            ]

            # Combine messages for content check
            messages_to_check = [m.content for _, m in self.user_message_cache[user_id]] or [message.content]
            combined_content = " ".join(messages_to_check)

            # Check message category
            category = mysql.check_offensive_message(combined_content, not_null=True)
            if category is None:
                category = await nsfw.moderator_api(text=combined_content, guild_id=guild_id)
            mysql.cache_offensive_message(combined_content, category)

            # If offensive, delete messages
            if category:
                cached_messages = self.user_message_cache[user_id]
                if cached_messages:
                    for _, msg in cached_messages:
                        try:
                            await msg.delete()
                        except (discord.Forbidden, discord.NotFound):
                            print("Cannot delete cached message.")
                    self.user_message_cache[user_id].clear()
                else:
                    try:
                        await message.delete()
                    except (discord.Forbidden, discord.NotFound):
                        print("Cannot delete new message.")
        
        # Handle NSFW content (not text)
        if (mysql.get_settings(message.guild.id, "delete-nsfw") == True) and not message.channel.id in mysql.get_settings(message.guild.id, "exclude-channels"):           
            if await nsfw.is_nsfw(message, self.bot, nsfw.handle_nsfw_content):
                try:
                    await message.delete()
                    await message.channel.send(
                        f"{message.author.mention}, your message was detected to contain explicit content and was removed."
                    )
                except (discord.Forbidden, discord.NotFound):
                    print("Bot does not have permission to delete the message or the message no longer exists.")

async def setup(bot: commands.Bot):
    await bot.add_cog(AggregatedModeration(bot))
