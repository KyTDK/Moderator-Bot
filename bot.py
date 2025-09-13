from discord.ext import commands, tasks
import discord
import os
import time
import logging
import asyncio
from dotenv import load_dotenv
from modules.utils import mysql
from modules.post_stats.topgg_poster import start_topgg_poster

print(f"[BOOT] Starting Moderator Bot at {time.strftime('%X')}")

# Ensure we see discord.py warnings/errors in Docker logs
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
)
logging.getLogger("discord").setLevel(logging.INFO)

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
    try:
        await mysql.initialise_and_get_pool()
    except Exception as e:
        print(f"[FATAL] MySQL init failed: {e}")
        raise

    # Start cleanup (non-blocking, will wait for ready inside the loop)
    try:
        cleanup_task.start()
    except RuntimeError:
        # task already started; ignore
        pass

    # Load cogs
    try:
        for filename in os.listdir('./cogs'):
            path = os.path.join('cogs', filename)
            if os.path.isfile(path) and filename.endswith('.py'):
                try:
                    await bot.load_extension(f'cogs.{filename[:-3]}')
                    print(f"Loaded Cog: {filename[:-3]}")
                except Exception as cog_err:
                    print(f"[FATAL] Failed to load cog {filename}: {cog_err}")
                    raise
            else:
                # Skip directories like __pycache__
                if filename not in ('__pycache__',):
                    print("Unable to load pycache folder.")
    except Exception:
        # Re-raise to let main capture and print a full traceback
        raise

    # Start Top.gg poster
    try:
        start_topgg_poster(bot)
    except Exception as e:
        print(f"[WARN] top.gg poster could not start: {e}")

    # Sync command tree (don't crash hard if this fails)
    try:
        await bot.tree.sync(guild=None)
        print("[SYNC] Application commands synced (global).")
    except Exception as e:
        print(f"[ERROR] Command tree sync failed: {e}")

    print("[SETUP] setup_hook completed.")

async def _main():
    if not TOKEN:
        print("[FATAL] DISCORD_TOKEN is not set. Exiting.")
        return
    try:
        await bot.start(TOKEN)
    except KeyboardInterrupt:
        pass
    except Exception as e:
        # Print full traceback so we can see the crash reason in docker logs
        print(f"[FATAL] Bot crashed: {e}")
        import traceback
        traceback.print_exc()
    finally:
        try:
            await mysql.close_pool()
        except Exception:
            pass

if __name__ == "__main__":
    asyncio.run(_main())
