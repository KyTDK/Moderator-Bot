from typing import Optional
import discord
from discord import app_commands
from discord.ext import commands
from modules.utils import mysql

class PrivacyCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="delete_my_data", description="Delete all your stored messages from the cache.")
    async def delete_my_data(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if mysql.delete_user_data(interaction.user.id):
            await interaction.followup.send("Your data was successfully deleted, please keep in mind this doesn't stop your data from being logged.")
        else:
            await interaction.followup.send("Something went wrong, please contact a server administrator.")
            
async def setup(bot: commands.Bot):
    await bot.add_cog(PrivacyCog(bot))
