from discord.ext import commands
import discord
import os
from dotenv import load_dotenv
from modules.utils import mysql
from modules.post_stats.topgg_poster import start_topgg_poster
import time

print(f"[BOOT] Starting Moderator Bot at {time.strftime('%X')}")

load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix='/', intents=intents, help_command=None)

@bot.event
async def on_ready():
    print(f"Bot connected as {bot.user} in {len(bot.guilds)} guilds")

@bot.event
async def on_resumed():
    print(">> Gateway session resumed.")

@bot.event
async def on_disconnect():
    print(">> Disconnected from gateway.")

@bot.event
async def on_connect():
    print(">> Connected to gateway.")

@bot.event
async def on_guild_join(guild):
    welcome_message = (
        "👋 **Thanks for adding Moderator Bot!**\n\n"
        "We're excited to be part of your server! 🎉 Moderator Bot works out of the box — no setup is required to start moderating effectively.\n\n"
        "📖 **Next Steps:**\n"
        "Use `/help` to explore all the commands and features. For any support, our Discord server is linked at the bottom of the help page.\n\n"
        "⚙️ **How Does This Work?**\n"
        "Moderator Bot uses AI to help moderate messages — and thanks to our **shared API key pool**, it can do this right away, without needing any setup on your end.\n\n"
        "🔄 **Want to Help Keep It Free & Fast for Everyone?**\n"
        "Contributing your OpenAI API key to the **shared pool** is **completely optional** and **won't use any of your credits**, as the moderation model is free. However, your OpenAI account must have at least $5 in prepaid credits to contribute. You can add credit here: <https://platform.openai.com/account/billing/overview>\n\n"
        "**To contribute your key (takes less than a minute):**\n"
        "1. Visit: <https://platform.openai.com/account/api-keys>\n"
        "2. Click **'Create new secret key'**\n"
        "3. Copy the generated key\n"
        "4. Run this command in your server:\n"
        "`/api_pool add YOUR_API_KEY_HERE`\n\n"
        "💡 Want to learn more?\n"
        "Run `/api_pool explanation` to see how the system works and how your contribution helps.\n\n"
        "🔒 **Privacy First:**\n"
        "All API keys are encrypted. No user messages or personal data are stored — only moderation-related settings and strike data are saved.\n\n"
        "🛠️ **Open Source & Community-Driven:**\n"
        "Moderator Bot is fully open source. Check it out or contribute on GitHub:\n"
        "<https://github.com/KyTDK/Moderator-Bot>\n\n"
        "Thanks for using Moderator Bot — let's build safer, more positive communities together! 🚀"
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
    await bot.tree.sync()
    start_topgg_poster(bot)
    await mysql.initialise_and_get_pool()

if __name__ == "__main__":
    bot.run(TOKEN)