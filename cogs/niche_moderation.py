import discord
from discord.ext import commands
from modules.utils import mysql

class NicheModerationCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def handle_message(self, message: discord.Message):
        if not message.guild or message.author.bot:
            return
        
        guild_id = message.guild.id
        role_id = await mysql.get_settings(guild_id, "no-forward-from-role")

        if role_id in [r.id for r in message.author.roles]:
            if getattr(message, "message_snapshots", []):
                await message.delete()

async def setup(bot: commands.Bot):
    await bot.add_cog(NicheModerationCog(bot))