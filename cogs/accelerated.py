import discord
from discord.ext import commands
from discord import Color, Embed, Interaction, app_commands, ButtonStyle
from discord.utils import format_dt, utcnow
from modules.utils import mysql
from datetime import timezone
from discord.ui import View, Button
from modules.core.moderator_bot import ModeratorBot
from modules.i18n.strings import locale_string

class AcceleratedCog(commands.Cog):
    """Commands for Moderator Bot Accelerated (Premium)."""

    def __init__(self, bot: ModeratorBot):
        self.bot = bot

    accelerated_group = app_commands.Group(
        name="accelerated",
        description=locale_string("cogs.accelerated.meta.group_description"),
    )

    @accelerated_group.command(
        name="status",
        description=locale_string("cogs.accelerated.meta.status.description"),
    )
    async def status(self, interaction: Interaction):
        """Check if you currently have an Accelerated subscription."""
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild_id = interaction.guild.id

        details = await mysql.get_premium_status(guild_id=guild_id)
        texts = self.bot.translate("cogs.accelerated.status", 
                                   guild_id=guild_id)
        fields = texts["field_names"]
        footer_base = texts["footer_base"]

        embed = Embed(
            title=texts["embed_title"],
            color=Color.red(),
        )

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
            not_enrolled = texts["not_enrolled"]
            embed.description = not_enrolled["description"]
            embed.add_field(name=fields["tier"], value=not_enrolled["tier_value"], inline=True)
            embed.add_field(name=fields["status"], value=not_enrolled["status_value"], inline=True)
            embed.add_field(
                name=fields["next_billing"],
                value=not_enrolled["next_billing_value"],
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

        states = texts["states"]
        if status_raw == "active" and is_active:
            state = states["active"]
            embed.color = Color.green()
        elif status_raw == "cancelled" and next_billing and is_active:
            state = states["cancelled"]
            embed.color = Color.orange()
        else:
            state = states["paused"]
            embed.color = Color.red()

        status_label = state["label"].format(status=status_raw.replace("_", " ").title())
        status_emoji = state["emoji"]
        embed.description = state["description"]

        embed.add_field(name=fields["tier"], value=f"`{tier_label}`", inline=True)
        embed.add_field(name=fields["status"], value=f"{status_emoji} {status_label}", inline=True)

        if next_billing:
            embed.add_field(
                name=fields["next_billing"],
                value=f"{format_dt(next_billing, style='F')}\n{format_dt(next_billing, style='R')}",
                inline=False,
            )
            embed.timestamp = next_billing
            embed.set_footer(text=texts["footers"]["next"].format(base=footer_base))
        else:
            embed.add_field(name=fields["next_billing"], value=texts["no_renewal"], inline=False)
            embed.timestamp = utcnow()
            embed.set_footer(text=texts["footers"]["checked"].format(base=footer_base))

        await interaction.followup.send(embed=embed, ephemeral=True)

    @accelerated_group.command(
        name="subscribe",
        description=locale_string("cogs.accelerated.meta.subscribe.description"),
    )
    async def subscribe(self, interaction: Interaction):
        """Generate your unique PayPal subscription link."""
        user_id = interaction.user.id
        guild_id = interaction.guild.id
        url = f"https://modbot.neomechanical.com/subscribe?gid={guild_id}"

        await interaction.response.defer(ephemeral=True, thinking=True)

        is_accelerated = await mysql.is_accelerated(guild_id=guild_id)
        subscribe_texts = self.bot.translate("cogs.accelerated.subscribe",
                                             guild_id=guild_id)
        if is_accelerated:
            return await interaction.followup.send(
                subscribe_texts["already_subscribed"],
                ephemeral=True
            )

        embed_texts = subscribe_texts["embed"]
        embed = Embed(
            title=embed_texts["title"],
            description=embed_texts["description"],
            color=Color.green()
        )
        embed.set_footer(text=embed_texts["footer"])
        if self.bot.user:
            embed.set_thumbnail(url=self.bot.user.display_avatar.url)

        view = View()
        view.add_item(Button(label=embed_texts["button_label"], url=url, style=ButtonStyle.link))

        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @accelerated_group.command(
        name="perks",
        description=locale_string("cogs.accelerated.meta.perks.description"),
    )
    async def perks(self, interaction: Interaction):
        """Show the benefits of Accelerated subscription."""
        guild_id = interaction.guild.id
        texts = self.bot.translate("cogs.accelerated.perks",
                                   guild_id=guild_id)
        embed = discord.Embed(
            title=texts["title"],
            description=texts["description"],
            color=discord.Color.gold()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @accelerated_group.command(
        name="cancel",
        description=locale_string("cogs.accelerated.meta.cancel.description"),
    )
    async def cancel(self, interaction: Interaction):
        """Explain how to cancel your subscription."""
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild.id
        message = self.bot.translate("cogs.accelerated.cancel.message", 
                                     guild_id=guild_id)
        await interaction.followup.send(message, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(AcceleratedCog(bot))