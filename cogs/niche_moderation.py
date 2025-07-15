import discord
from discord.ext import commands
from modules.utils import mysql

class NicheModerationCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def handle_message(self, message: discord.Message):
        if not message.guild or message.author.bot:
            return
        
        raw = await mysql.get_settings(message.guild.id, "no-forward-from-role")
        role_ids = {int(raw)} if isinstance(raw, int) else {int(rid) for rid in raw or []}

        if not isinstance(message.author, discord.Member):
            return

        if any(role.id in role_ids for role in message.author.roles) and getattr(message, "message_snapshots", []):
            await message.delete()

async def setup(bot: commands.Bot):
    await bot.add_cog(NicheModerationCog(bot))