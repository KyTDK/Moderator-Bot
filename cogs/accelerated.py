import discord
from discord.ext import commands
from discord import Color, Embed, Interaction, app_commands, ButtonStyle
from modules.utils import mysql
import aiohttp
from discord.ui import View, Button

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
        backend_url = f"https://modbot.neomechanical.com/subscribe?gid={guild_id}"

        await interaction.response.defer(ephemeral=True, thinking=True)

        # Check if already subscribed
        is_accelerated = await mysql.is_accelerated(guild_id=guild_id)
        if is_accelerated:
            print(f"[Accelerated] User {user_id} in guild {guild_id} already subscribed.")
            return await interaction.followup.send(
                "✅ Your guild already has an active **Accelerated** subscription!",
                ephemeral=True
            )

        # Request subscription link from backend
        async with aiohttp.ClientSession() as session:
            async with session.get(backend_url) as resp:
                if resp.status != 200:
                    print(f"[ERROR] Failed to generate subscription link for user {user_id} in guild {guild_id}")
                    return await interaction.followup.send(
                        "⚠️ Could not generate a subscription link. Please try again later.",
                        ephemeral=True
                    )
                data = await resp.json()

        approve_url = data.get("url")
        if not approve_url:
            return await interaction.followup.send(
                "⚠️ Something went wrong generating your subscription link.",
                ephemeral=True
            )

        # Fancy Embed
        embed = Embed(
            title="🚀 Upgrade to Moderator Bot Accelerated!",
            description=(
                "**Enjoy blazing-fast NSFW & scam detection, priority queues, and early access to new features!**\n\n"
                "💎 **1-Month Free Trial for New Subscribers!**\n"
                "After your free month, continue for just **$5/month** to keep your server safer, faster."
            ),
            color=Color.green()
        )
        embed.set_footer(text="Moderator Bot • Accelerated Plan")
        embed.set_thumbnail(url=self.bot.user.display_avatar.url)

        # Subscription button
        view = View()
        view.add_item(Button(label="🔗 Subscribe Now", url=approve_url, style=ButtonStyle.link))

        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @accelerated_group.command(name="perks")
    async def perks(self, interaction: Interaction):
        """Show the benefits of Accelerated subscription."""
        embed = discord.Embed(
            title="Accelerated Perks",
            description=(
                "Upgrade your experience with Moderator Bot Accelerated:\n"
                "• AI-powered autonomous moderation scanning to detect and act on rule-breaking messages automatically\n"
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