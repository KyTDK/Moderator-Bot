from discord.ext import commands
from discord import Color, Embed, Interaction, app_commands

class DashboardCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="dashboard", description="Open the dashboard for this server.")
    async def dashboard(self, interaction: Interaction):
        """Open the dashboard for this server."""
        guild_id = interaction.guild.id
        backend_url = f"https://modbot.neomechanical.com/dashboard/{guild_id}"

        await interaction.response.defer(ephemeral=True, thinking=True)

        # Return dashboard link
        embed = Embed(
            title="Moderator Bot Dashboard",
            description=f"[Click here to open the dashboard]({backend_url})",
            color=Color.blurple()
        )
        embed.set_thumbnail(url=self.bot.user.display_avatar.url)
        await interaction.followup.send(embed=embed, ephemeral=True)
async def setup(bot: commands.Bot):
    await bot.add_cog(DashboardCog(bot))