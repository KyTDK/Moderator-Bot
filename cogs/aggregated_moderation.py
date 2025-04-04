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
        self.AGGREGATION_WINDOW = 20  # seconds

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Skip messages sent by bots to prevent potential loops
        if message.author.bot:
            return

        user_id = message.author.id

        # If multiple messages exist in the cache, combine them for moderation check
        if mysql.get_settings(message.guild.id, "delete-offensive") == "True" or \
        (mysql.get_strike_count(message.author.id, message.guild.id) > 0 and mysql.get_settings(message.guild.id, "restrict-striked-users") == "True"):
            now = time.time()

            # Add current message to cache
            self.user_message_cache[user_id].append((now, message))

            # Remove messages older than the aggregation window
            self.user_message_cache[user_id] = [
                (t, m) for t, m in self.user_message_cache[user_id] if now - t < self.AGGREGATION_WINDOW
            ]
            if len(self.user_message_cache[user_id]) > 0:
                combined_content = " ".join([m.content for _, m in self.user_message_cache[user_id]])
                if nsfw.moderator_api(combined_content):
                    # Delete all cached messages
                    for _, msg in self.user_message_cache[user_id]:
                        try:
                            await msg.delete()
                        except (discord.Forbidden, discord.NotFound):
                            # Either bot lacks permissions or the message has already been deleted.
                            print("Bot does not have permission to delete a message or the message no longer exists.")
                    # Clear cache for this user
                    self.user_message_cache[user_id].clear()

        if await nsfw.is_nsfw(message, self.bot, nsfw.handle_nsfw_content) and mysql.get_settings(message.guild.id, "delete-offensive") == "True" and not message.channel.id in mysql.get_settings(message.guild.id, "exlude-channels"):
            try:
                await message.delete()
                await message.channel.send(
                    f"{message.author.mention}, your message was detected to contain explicit content and was removed."
                )
            except (discord.Forbidden, discord.NotFound):
                print("Bot does not have permission to delete the message or the message no longer exists.")

async def setup(bot: commands.Bot):
    await bot.add_cog(AggregatedModeration(bot))
