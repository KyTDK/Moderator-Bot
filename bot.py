from discord.ext import commands, tasks
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
intents.presences = True
intents.message_content = True
bot = commands.Bot(command_prefix=lambda b, m: [],
                   intents=intents,
                   chunk_guilds_at_startup=False, 
                   member_cache_flags=discord.MemberCacheFlags.none(),
                   help_command=None,
                   max_messages=None)

# Cleanup schedule
@tasks.loop(hours=6)
async def cleanup_task():
    await bot.wait_until_ready()
    guild_ids = [g.id for g in bot.guilds]
    print(f"[CLEANUP] Running cleanup for {len(guild_ids)} guilds...")

    await mysql.cleanup_orphaned_guilds(guild_ids)
    await mysql.cleanup_expired_strikes()

@bot.event
async def on_ready():
    print(f"Bot connected as {bot.user} in {len(bot.guilds)} guilds")

    # Sync guilds with the database
    for guild in bot.guilds:
        try:
            await mysql.add_guild(guild.id, guild.name, guild.owner_id)
        except Exception as e:
            print(f"[ERROR] Failed to sync guild {guild.id}: {e}")
    print(f"Synced {len(bot.guilds)} guilds with the database.")
    

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
async def on_guild_join(guild: discord.Guild):
    # Update DB
    await mysql.add_guild(guild.id, guild.name, guild.owner_id)
    dash_url = f"https://modbot.neomechanical.com/dashboard/{guild.id}"

    welcome_message = f"""
    👋 **Thanks for adding Moderator Bot!**

    🛠️ **Dashboard:** [Open Dashboard]({dash_url})

    **Quick start**
    • Run **`/help`** to see commands (try `/help nsfw`, `/help strikes`)
    • Use the **Dashboard** to configure thresholds, actions, and toggles

    **Works out of the box**
    AI moderation is enabled with sane defaults. You can fine-tune anything in the Dashboard.

    **Need help?**
    Open the Dashboard above, or run **`/help`** for details and the support link.
    """

    view = discord.ui.View()
    view.add_item(discord.ui.Button(
        label="Open Dashboard",
        url=dash_url,
        emoji="🛠️",
    ))
    # Attempt to send the message to the system channel
    if guild.system_channel and guild.system_channel.permissions_for(guild.me).send_messages:
        try:
            await guild.system_channel.send(welcome_message, view=view)
            return
        except discord.Forbidden:
            pass  # Proceed to find another channel

    # Fallback: Find the first text channel where the bot has permission to send messages
    for channel in guild.text_channels:
        if channel.permissions_for(guild.me).send_messages:
            try:
                await channel.send(welcome_message, view=view)
                break
            except discord.Forbidden:
                continue

@bot.event
async def on_guild_remove(guild: discord.Guild):
    # Remove guild from DB
    await mysql.remove_guild(guild.id)

@bot.event
async def setup_hook():
    # Initialize the MySQL connection pool
    await mysql.initialise_and_get_pool()
    
    # Start cleanup
    cleanup_task.start()
    
    # Start cogs
    for filename in os.listdir('./cogs'):
        if filename.endswith('.py'):
            await bot.load_extension(f'cogs.{filename[:-3]}')
            print(f"Loaded Cog: {filename[:-3]}")
        else:
            print("Unable to load pycache folder.")

    # Start Top.gg poster
    start_topgg_poster(bot)

    # Sync command tree
    await bot.tree.sync(guild=None)

if __name__ == "__main__":
    bot.run(TOKEN)