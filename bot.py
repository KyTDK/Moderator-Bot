from discord.ext import commands
import discord
import os
from discord import app_commands
from dotenv import load_dotenv
from modules.detection import nsfw
from modules.utils import user_utils, mysql

load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix='/', intents=intents)

@bot.event
async def on_ready():
    await bot.tree.sync()
    mysql.initialize_database()


@bot.event
async def on_message(message: discord.Message):
    
    # Skip messages sent by bots to prevent potential loops
    if message.author.bot:
        return
    
    # Check NSFW
    if await nsfw.is_nsfw(message, bot, nsfw.handle_nsfw_content):
        try:
            await message.delete()
            await message.channel.send(
                f"{message.author.mention}, your message was detected to contain explicit content and was removed."
            )
        except discord.Forbidden:
            print("Bot does not have permission to delete messages.")


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