from discord.ext import commands
from discord import Color, Embed, Interaction, app_commands
from modules.core.moderator_bot import ModeratorBot

class DashboardCog(commands.Cog):
    def __init__(self, bot: ModeratorBot):
        self.bot = bot

    @app_commands.command(name="dashboard", description="Open the dashboard for this server.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def dashboard(self, interaction: Interaction):
        """Open the dashboard for this server."""
        guild_id = interaction.guild.id
        backend_url = f"https://modbot.neomechanical.com/dashboard/{guild_id}"

        await interaction.response.defer(ephemeral=True, thinking=True)

        embed = Embed(
            title=self.bot.translate("cogs.dashboard.embed.title",
                                     guild_id=guild_id),
            description=self.bot.translate(
                "cogs.dashboard.embed.description",
                placeholders={"url": backend_url},
                guild_id=guild_id,
            ),
            color=Color.blurple()
        )
        if self.bot.user:
            embed.set_thumbnail(url=self.bot.user.display_avatar.url)
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(DashboardCog(bot))
