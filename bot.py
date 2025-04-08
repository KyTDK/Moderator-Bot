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
bot = commands.Bot(command_prefix='/', intents=intents)

async def make_announcement(guild_id, message):
    guild = bot.get_guild(guild_id)
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
        #await bot.tree.sync(guild=guild)
    total_users_not_bots = sum(len([member for member in guild.members if not member.bot]) for guild in bot.guilds)
    print(f"Connected to {len(bot.guilds)} guilds with a total of {total_users_not_bots} users.")

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