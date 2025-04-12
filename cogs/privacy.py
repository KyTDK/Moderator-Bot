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
            await interaction.followup.send("No data was found to delete.")

    @app_commands.command(name="opt_in", description="Opt-in to data storage for improved moderation")
    async def opt_in(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if mysql.opt_in_user(interaction.user.id):
            await interaction.followup.send("✅ You have successfully **opted in** to data storage. Your messages will now be cached to enhance moderation efficiency.")
        else:
            await interaction.followup.send("ℹ️ You are already **opted in** to data storage.")

    @app_commands.command(name="opt_out", description="Opt-out of data storage to protect your privacy")
    async def opt_out(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if mysql.opt_out_user(interaction.user.id):
            await interaction.followup.send("✅ You have successfully **opted out** of data storage. Your future messages will no longer be cached.")
        else:
            await interaction.followup.send("ℹ️ You are already **opted out** of data storage.")
            
async def setup(bot: commands.Bot):
    await bot.add_cog(PrivacyCog(bot))
