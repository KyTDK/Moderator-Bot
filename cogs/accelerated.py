import discord
from discord.ext import commands
from discord import Color, Embed, Interaction, app_commands, ButtonStyle
from discord.utils import format_dt, utcnow
from modules.utils import mysql
import aiohttp
from datetime import timezone
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
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild_id = interaction.guild.id

        details = await mysql.get_premium_status(guild_id=guild_id)

        embed = Embed(
            title="Accelerated Status",
            color=Color.red(),
        )
        footer_base = "Moderator Bot Accelerated"

        guild_icon_url = interaction.guild.icon.url if interaction.guild and interaction.guild.icon else None
        bot_avatar_url = self.bot.user.display_avatar.url if self.bot.user else None
        display_icon_url = guild_icon_url or bot_avatar_url
        display_name = interaction.guild.name if interaction.guild else "Moderator Bot"

        if display_icon_url:
            embed.set_author(name=display_name, icon_url=display_icon_url)
            embed.set_thumbnail(url=display_icon_url)
        else:
            embed.set_author(name=display_name)

        if not details:
            embed.description = (
                "This server is not enrolled in **Accelerated** yet. Subscribe now with, "
                "with `/accelerated subscribe`."
            )
            embed.add_field(name="Tier", value="`None`", inline=True)
            embed.add_field(name="Status", value="`Not Subscribed`", inline=True)
            embed.add_field(
                name="Next Billing",
                value="Start a plan with `/accelerated subscribe`",
                inline=False,
            )
            embed.timestamp = utcnow()
            embed.color = Color.red()
            embed.set_footer(text=footer_base)
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        tier_raw = details.get("tier") or "accelerated"
        tier_label = tier_raw.replace("_", " ").title()
        status_raw = (details.get("status") or "unknown").lower()
        is_active = details.get("is_active", False)
        next_billing = details.get("next_billing")

        if next_billing and next_billing.tzinfo is None:
            next_billing = next_billing.replace(tzinfo=timezone.utc)

        if status_raw == "active" and is_active:
            status_label = "Active"
            status_emoji = ":green_circle:"
            embed.color = Color.green()
            embed.description = "Accelerated is active."
        elif status_raw == "cancelled" and next_billing and is_active:
            status_label = "Active - Cancels Soon"
            status_emoji = ":orange_circle:"
            embed.color = Color.orange()
            embed.description = "Accelerated will remain active until the end of this billing cycle."
        else:
            status_label = status_raw.replace("_", " ").title()
            status_emoji = ":red_circle:"
            embed.color = Color.red()
            embed.description = "Accelerated perks are paused. Restart anytime with `/accelerated subscribe`."

        embed.add_field(name="Tier", value=f"`{tier_label}`", inline=True)
        embed.add_field(name="Status", value=f"{status_emoji} {status_label}", inline=True)

        if next_billing:
            embed.add_field(
                name="Next Billing",
                value=f"{format_dt(next_billing, style='F')}\n{format_dt(next_billing, style='R')}",
                inline=False,
            )
            embed.timestamp = next_billing
            embed.set_footer(text=f"{footer_base} - Next billing cycle")
        else:
            embed.add_field(name="Next Billing", value="`No renewal scheduled`", inline=False)
            embed.timestamp = utcnow()
            embed.set_footer(text=f"{footer_base} - Status checked")

        await interaction.followup.send(embed=embed, ephemeral=True)

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
