from discord.ext import commands
import discord
import os
from dotenv import load_dotenv
from modules.utils import mysql

load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix='/', intents=intents, help_command=None)

async def make_announcement(guild, message):
    if guild:
        channel = guild.system_channel  # Default system channel for announcements
        if channel:
            try:
                await channel.send(message)
            except discord.Forbidden:
                print(f"Cannot send message to {channel.name} in {guild.name}. Check permissions.")

@bot.event
async def on_ready():
    mysql.initialize_database()
    # show info of guilds the bot is in
    for guild in bot.guilds:
        print(f"Connected to {guild.name} (ID: {guild.id}) with {len([member for member in guild.members if not member.bot])} members ")
    total_users_not_bots = sum(len([member for member in guild.members if not member.bot]) for guild in bot.guilds)
    await bot.tree.sync()
    print(f"Connected to {len(bot.guilds)} guilds with a total of {total_users_not_bots} users.")

@bot.event
async def setup_hook():
    for filename in os.listdir('./cogs'):
        if filename.endswith('.py'):
            await bot.load_extension(f'cogs.{filename[:-3]}')
            print(f"Loaded Cog: {filename[:-3]}")
        else:
            print("Unable to load pycache folder.")

@bot.event
async def on_guild_join(guild):
    privacy_message = (
        "Hello! 👋\n\n"
        "I'm your new moderation bot. Here's how I handle your data:\n\n"
        "🔐 **Data Storage**: I store message content to help categorize messages as safe or not safe. "
        "User IDs are stored in an encrypted format to ensure your privacy.\n\n"
        "🗑️ **Data Deletion**: You can delete all data associated with your account at any time by using the `/delete_my_data` command.\n\n"
        "🔄 **Opting In/Out**: You have control over your data. Use `/opt_out` to prevent any future data from being stored, or `/opt_in` to allow data storage again.\n\n"
        "⚙️ **Server-Wide Settings**: Server administrators can disable data storage for all members by setting the `opt-in` setting to `False` using the `/settings set opt-in False` command.\n\n"
        "If you have any questions or concerns about your data, feel free to reach out to the server administrators."
    )

    # Attempt to send the message to the system channel
    if guild.system_channel and guild.system_channel.permissions_for(guild.me).send_messages:
        await guild.system_channel.send(privacy_message)
    else:
        # Fallback: find the first channel where the bot has permission to send messages
        for channel in guild.text_channels:
            if channel.permissions_for(guild.me).send_messages:
                await channel.send(privacy_message)
                break

if __name__ == "__main__":
    bot.run(TOKEN)