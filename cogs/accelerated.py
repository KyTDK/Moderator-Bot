import discord
from discord.ext import commands
from discord import Interaction, app_commands
from modules.utils import mysql
import aiohttp

class AcceleratedCog(commands.Cog):
    """Commands for Moderator Bot Accelerated (Premium)."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    accelerated_group = app_commands.Group(
        name="accelerated",
        description="Manage your Accelerated (Premium) subscription.",
    )

    @accelerated_group.command(name="status")
    async def status(self, interaction: Interaction):
        """Check if you currently have an Accelerated subscription."""
        guild_id = interaction.guild.id
        is_accelerated = await mysql.is_accelerated(guild_id=guild_id)

        if not is_accelerated:
            await interaction.response.send_message(
                "This server doesn't have an Accelerated subscription. Use `/accelerated subscribe` to start your premium plan.",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "This server has an active Accelerated subscription.",
                ephemeral=True
            )

    @accelerated_group.command(name="subscribe")
    async def subscribe(self, interaction: Interaction):
        """Generate your unique PayPal subscription link."""
        user_id = interaction.user.id
        guild_id = interaction.guild.id
        backend_url = f"https://modbot.neomechanical.com/api/create-subscription?gid={guild_id}&uid={user_id}"

        await interaction.response.defer(ephemeral=True, thinking=True)

        # Check if the user already has a subscription
        is_accelerated = await mysql.is_accelerated(guild_id=guild_id)
        if is_accelerated:
            print(f"[Accelerated] User {user_id} in guild {guild_id} already has an active subscription.")
            return await interaction.followup.send(
                "You already have an active Accelerated subscription.",
                ephemeral=True
            )

        async with aiohttp.ClientSession() as session:
            async with session.get(backend_url) as resp:
                if resp.status != 200:
                    print(f"[ERROR] Failed to generate subscription link for user {user_id} in guild {guild_id}")
                    return await interaction.followup.send(
                        "Failed to generate subscription link. Please try again later.",
                        ephemeral=True
                    )
                data = await resp.json()

        approve_url = data.get("url")
        if not approve_url:
            print(f"[ERROR] Failed to get approve URL for user {user_id} in guild {guild_id}")
            return await interaction.followup.send(
                "Something went wrong generating your subscription link.",
                ephemeral=True
            )

        await interaction.followup.send(
            f"Click here to subscribe:\n{approve_url}"
            )

    @accelerated_group.command(name="perks")
    async def perks(self, interaction: Interaction):
        """Show the benefits of Accelerated subscription."""
        embed = discord.Embed(
            title="Accelerated Perks",
            description=(
                "Upgrade your experience with Moderator Bot Accelerated:\n"
                "• Scan more frames per video for deeper NSFW detection\n"
                "• Priority NSFW & Scam detection\n"
                "• Early access to new features\n"
                "• Supports the bot's development"
            ),
            color=discord.Color.gold()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @accelerated_group.command(name="cancel")
    async def cancel(self, interaction: Interaction):
        """Explain how to cancel your subscription."""
        await interaction.response.send_message(
            "You can cancel your PayPal subscription anytime via your **PayPal account → Settings → Payments → Manage Automatic Payments**.\n"
            "Your premium access will last until the end of the billing cycle.",
            ephemeral=True
        )

async def setup(bot: commands.Bot):
    await bot.add_cog(AcceleratedCog(bot))