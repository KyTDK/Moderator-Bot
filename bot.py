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
    print(f"Connected to {len(bot.guilds)} guilds with a total of {total_users_not_bots} users.")

@bot.event
async def on_guild_join(guild):
    welcome_message = (
        "👋 **Thanks for adding Moderator Bot!**\n\n"
        "We're thrilled to be part of your server. To ensure optimal performance, especially with our AI-powered moderation features, we recommend setting up your own OpenAI API key. This helps prevent potential rate limits due to high usage.\n\n"
        "**Setting up is easy and free:**\n"
        "1. Visit: https://platform.openai.com/account/api-keys\n"
        "2. Click on 'Create new secret key' and follow the prompts.\n"
        "3. Copy the generated API key.\n"
        "4. In your server, use the command:\n"
        "`/settings set api-key YOUR_API_KEY_HERE`\n"
        "Replace `YOUR_API_KEY_HERE` with the key you obtained.\n\n"
        "🔒 **Privacy Notice:**\n"
        "We've enhanced our privacy measures. All previously logged user data has been deleted to ensure compliance with privacy standards. Consequently, opt-in and opt-out features have been removed. Moderator Bot no longer tracks user data.\n\n"
        "If you have any questions or need assistance, feel free to reach out. Thank you for choosing Moderator Bot!"
    )

    # Attempt to send the message to the system channel
    if guild.system_channel and guild.system_channel.permissions_for(guild.me).send_messages:
        try:
            await guild.system_channel.send(welcome_message)
            return
        except discord.Forbidden:
            pass  # Proceed to find another channel

    # Fallback: Find the first text channel where the bot has permission to send messages
    for channel in guild.text_channels:
        if channel.permissions_for(guild.me).send_messages:
            try:
                await channel.send(welcome_message)
                break
            except discord.Forbidden:
                continue

@bot.event
async def setup_hook():
    for filename in os.listdir('./cogs'):
        if filename.endswith('.py'):
            await bot.load_extension(f'cogs.{filename[:-3]}')
            print(f"Loaded Cog: {filename[:-3]}")
        else:
            print("Unable to load pycache folder.")

if __name__ == "__main__":
    bot.run(TOKEN)