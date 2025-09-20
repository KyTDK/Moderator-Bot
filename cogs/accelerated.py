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
                ephemeral=True,
            )
            return

        # Accelerated is currently granted; see if it is cancelled-but-active-until <date>
        details = await mysql.get_premium_status(guild_id=guild_id)
        if details and details.get("status") == "cancelled" and details.get("next_billing"):
            end_dt = details["next_billing"]
            # Format as YYYY-MM-DD HH:MM UTC
            try:
                end_fmt = end_dt.strftime("%Y-%m-%d %H:%M UTC")
            except Exception:
                end_fmt = str(end_dt)
            await interaction.response.send_message(
                f"This server's Accelerated subscription is cancelled, but remains active until {end_fmt}.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "This server has an active Accelerated subscription.",
            ephemeral=True,
        )

    @accelerated_group.command(name="subscribe")
    async def subscribe(self, interaction: Interaction):
        """Generate your unique PayPal subscription link."""
        user_id = interaction.user.id
        guild_id = interaction.guild.id
        url = f"https://modbot.neomechanical.com/subscribe?gid={guild_id}"

        await interaction.response.defer(ephemeral=True, thinking=True)

        # Check if already subscribed
        is_accelerated = await mysql.is_accelerated(guild_id=guild_id)
        if is_accelerated:
            print(f"[Accelerated] User {user_id} in guild {guild_id} already subscribed.")
            return await interaction.followup.send(
                "✅ Your guild already has an active **Accelerated** subscription!",
                ephemeral=True
            )

        # Fancy Embed
        embed = Embed(
            title="🚀 Upgrade to Moderator Bot Accelerated!",
            description=(
                "**Enjoy blazing-fast NSFW & scam detection, priority queues, and early access to new features!**\n\n"
                "After your free month, continue for just **$5/month** to keep your server safer, faster."
            ),
            color=Color.green()
        )
        embed.set_footer(text="Moderator Bot • Accelerated Plan")
        embed.set_thumbnail(url=self.bot.user.display_avatar.url)

        # Subscription button
        view = View()
        view.add_item(Button(label="🔗 Subscribe Now", url=url, style=ButtonStyle.link))

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
